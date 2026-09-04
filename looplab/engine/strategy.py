"""Strategist cadence (A7) for the engine — the bounded-cadence meta-controller consult plus the
coverage-snapshot cadence — extracted from orchestrator.py as a MIXIN: `class Engine(…,
StrategyCadenceMixin)` inherits these methods unchanged, so there is ZERO call-site churn and
`self` here IS the engine. The method bodies are verbatim moves and read engine attributes freely
(`store` / `strategist` / `policy` / `researcher` / `developer_factory` / the `_policy_name`,
`_ablate_every`, `_coverage_context`, `strategist_every`, `n_seeds` … knobs), exactly as they did
inside the class.

Two clusters that used to sit here left in doc 25 EC-09, because neither is strategist cadence:
the PART IV/V concept subsystem (classifier re-tag / consolidation / edges / hypothesis tags /
concept-coverage snapshot / run-base seed) is `engine/concept_cadence.py`, and it paces on its OWN
`concept_retag_every` gate rather than `strategist_every`; the R1-c calibrated-verifier metric
tie-break is `engine/verifier_tiebreak.py`, and it is SELECTION machinery — it can move the run's
reported champion. What remains here is the consult/apply/coverage core: build the brief, validate
and merge the operator pin against the Strategist's decision, apply the knobs through the governance
matrix, and record the breadth snapshot the brief reads.

`_op_span` deliberately stays on the Engine: it is a generic new-trace span helper the research /
hypothesis-merge / lessons clusters use too, not strategist-specific. The moved methods call it as
`self._op_span(...)` — resolved on the Engine instance, unchanged.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only core,
events, search, agents and stdlib (SurrogateResearcher / cli PRESETS stay lazy, method-local)."""
from __future__ import annotations

import json
import math
from typing import Optional

from looplab.agents.strategist import (NOVELTY_STANCES, StrategyContext,
                                       canonicalize_strategy_parallelism, failure_rate,
                                       improves_since_best, is_numeric_space,
                                       classify_run_phase,
                                       validate_card_scoring, validate_strategy)
from looplab.core.config import parallelism_aliases
from looplab.core.llm_broker import LLM_LANES, in_llm_lane
from looplab.core.models import RunState
from looplab.engine.cadence import (at_creation_boundary, cadence_due, cadence_marks,
                                     seed_boundary_due)
from looplab.engine.widths import EVAL_WIDTH_MAX, LLM_WIDTH_MAX, settle_width
from looplab.engine.costs import bind_cost_accountants
from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.events.replay import fold
from looplab.events.types import EV_COVERAGE_SNAPSHOT, EV_STRATEGY_DECISION
from looplab.search.coverage import (already_covered_at, analytics_projection_token, coverage_signal,
                                     latest_live_snapshot)
from looplab.search.policy import available_policies, make_policy, operator_yields
from looplab.trust.cross_run import (cross_run_text, same_live_direction,
                                     sanitize_cross_run_projection, valid_live_direction)


_NO_PREPARED_DEVELOPER = object()


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
        s = canonicalize_strategy_parallelism(s)
        # `timeout`/`max_parallel` are here because _apply_strategy applies them (resource-budget retune);
        # omitting them meant a decision that changed ONLY one of them was never seen as a change, so it
        # was never recorded or applied (the P8 live-budget retune silently no-op'd unless bundled with a
        # change to another tracked field).
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity",
                                      "novelty_stance", "card_scoring", "request_research", "timeout", "max_parallel",
                                      "parallel_build", "eval_parallel", "llm_parallel",
                                      "llm_lane_limits")}

    def _available_developers(self) -> list[str]:
        """The developer-backend vocabulary the Strategist may choose from — and the ONLY gate on it,
        since `validate_strategy` drops a `developer` this list does not contain.

        Derived from `core.config`'s registry rather than re-spelled here: this list, the closed
        `DEVELOPER_BACKENDS` set `Settings` validates against, and the alias `make_developer_factory`
        resolves are three views of ONE vocabulary, and while they were three hand-written lists they
        disagreed (`agents/strategist.py` held an `"agentless"` arm that is in none of them and could
        never fire). Reading the registry also drops the `agents.cli_agent` import this engine module
        used to need for `PRESETS` — `DEVELOPER_BACKENDS` is asserted equal to it at that module's
        import and by `tests/test_developer_backend_registry.py`, in both directions.

        With no `developer_factory` wired nothing can be swapped, so only the in-house Developer is
        offered — otherwise every proposal would be dropped by validation instead of recording the
        `factory_unavailable` refusal `_prepare_strategy_developer` would have written."""
        from looplab.core.config import developer_switch_names
        names = list(developer_switch_names())
        return names if self.developer_factory is not None else ["default"]

    def _strategy_ctx(self, state: RunState) -> StrategyContext:
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        rem = (max_es - state.total_eval_seconds) if max_es is not None else None
        card_driven = bool(getattr(self, "card_driven_selection", False))
        if card_driven:
            from looplab.search.card_selection import card_budget_used
            node_budget_used = card_budget_used(state)
        else:
            node_budget_used = len(state.nodes)
        current_card_scoring = dict(getattr(self, "_card_scoring", {
            "stance": "balanced", "novelty_weight": 0.5, "coverage_weight": 0.5,
        }))
        defaults = {
            "policy": self._policy_name,
            "operators": {"ablate_every": self._ablate_every},
            "card_scoring": current_card_scoring,
        }
        if max_es:
            defaults["_budget_frac"] = max(0.0, (rem or 0.0) / max_es)
        # Mean per-node eval cost so far — the cost signal the Strategist uses to bias toward an
        # intra-node sweep (amortizing data load / warm-up pays off when each eval is expensive).
        ev = [n.eval_seconds for n in state.nodes.values() if n.eval_seconds]
        avg_es = (sum(ev) / len(ev)) if ev else None
        cross_run_note = self._cross_run_note_for_ctx(state)
        # A built Engine always owns the broker. Keep this accessor direct so a wiring typo fails
        # loudly instead of silently telling the Strategist that every lane is unbounded.
        llm_broker = self._llm_broker.snapshot()
        llm_lane_limits = llm_broker["lane_limits"]
        return StrategyContext(
            node_count=len(state.nodes),
            phase=classify_run_phase(state, self.n_seeds),
            eval_budget_remaining=rem,
            failure_rate=failure_rate(state),
            improves_since_best=improves_since_best(state),
            is_numeric_space=is_numeric_space(state),
            avg_eval_seconds=avg_es,
            node_budget_frac=(node_budget_used / self.policy.max_nodes
                              if getattr(self.policy, "max_nodes", 0) else 0.0),  # P2 endgame reserve
            current_policy=self._policy_name,   # D3: lets the rule switch BACK to greedy post-stall
            eval_parallel=self._eval_parallel,
            llm_parallel=self._llm_parallel,
            llm_total=llm_broker["total"],
            llm_lane_limits=llm_lane_limits,
            card_driven_selection=card_driven,
            card_scoring=current_card_scoring,
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
        from looplab.engine import cross_run_context as ctx
        if not ctx.advisory_enabled(self):
            self._cross_run_note_receipt = {}
            return ""
        current_direction = getattr(state, "direction", None) if state is not None else None
        # the Strategist consumes this as live decision context. No state or an invalid
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
            )
            from looplab.engine.memory import _capsule_source_summary, _filter_capsule_rows
            base = Path(self.memory_dir)
            if _governance is None:
                return ctx.enter_governed(
                    base,
                    lambda governance: self._cross_run_note_for_ctx(state, _governance=governance))
            lessons, caps, research = ctx.load_governed_sources(base)
            run_id, task_id = ctx.scoped_identity(state)
            _visible = ctx.visible_row_predicate(
                current_direction, task_id=task_id, excluded_run=run_id)
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
            if ctx.empty_after_complete_read(lessons, caps, research, capsule_source, claim_source):
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
            # the source receipt is model-visible AND part of both semantic/render digests.
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
            # zero mixed-evidence records is exact only when both evidence stores and the D8
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
            self._cross_run_note_receipt = ctx.build_receipt(
                scope_task=task_id, excluded_run=run_id,
                lessons=lessons, capsules=caps, research=research,
                scope_key="concept_source", scope_value=concept_source,
                claim_source=claim_receipt,
                corpus=ctx.corpus_digest({"parts": parts}, max_chars=16_000, max_items=64,
                                         max_total_items=256),
                rendered=note)
            return note
        except GovernanceLedgerUnavailable as exc:
            self._cross_run_note_receipt = ctx.unavailable_receipt(exc)
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
        # #5: reuse the newest STILL-LIVE snapshot (shared reverse-scan reader), not only the last row;
        # an off-cadence consult (or a stale receipt after a retag) computes fresh.
        snap = latest_live_snapshot(state, state.coverage_snapshots)
        if snap:
            return {k: v for k, v in snap.items() if k not in {"at_node", "projection_token"}}
        return coverage_signal(state, resolution=self.archive_resolution)

    def _should_consult(self, state: RunState, *, marks=None) -> bool:
        """Bounded, deterministic cadence: only at a creation decision point (no pending evals),
        at the seed boundary, then every `strategist_every` created nodes SINCE this consumer last
        fired.

        Since-last, not `n % every == 0`. Under `llm_parallel > 1` the node count advances in
        batch-width strides, so a modulo gate can step clean over the only multiple in a window and
        skip the consult entirely — with width 4 and `strategist_every=5` it can miss every multiple
        and starve the Strategist for the whole run. That is the same failure the deep-research
        cadence was patched for; `_cadence_due` carries the canonical statement of it.

        `marks` is the CONSUMER's own durable record of when it last fired (`at_node` on its folded
        events), because the three consumers of this gate — the Strategist consult, the coverage
        snapshot, the concept-coverage snapshot — advance independently. Passing none keeps the
        window open from node 0, which is right for a consumer that has never fired.

        "No pending evals" is the OBSERVABLE the creation decision point used to have and no longer
        does — since F1f the outer loop turns while adopted evaluations burn, and this gate answered
        NO for the whole life of every GPU run after `rubertlite-dr-unified-v8` (measured: 0
        quiescent prefixes on v7, v9 and the live `e5small-dr-unified-v2`; v8's own single window is
        the last 8.1 minutes of a 47.6-hour run). `cadence.at_creation_boundary` carries the
        measurement and the money rule; `_cadence_while_evaluating` is the kill switch back.
        """
        if not at_creation_boundary(len(state.pending_nodes()),
                                    while_evaluating=getattr(
                                        self, "_cadence_while_evaluating", False)):
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        last = cadence_marks(marks)
        # `>=`, not `== self.n_seeds` — the seed boundary is a count that advances in strides, so an
        # equality can be stepped over by a fan-out or a speculative prefetch. `strategist_every` is
        # short enough that a missed boundary here only DELAYS the first consult, which is why this
        # went unnoticed; the same equality in `_should_consult_concepts` cancelled that phase for the
        # whole run. One spelling for both, in `cadence.seed_boundary_due`.
        if seed_boundary_due(n, last, self.n_seeds):
            return True
        # `strategist_every` is `ge=1` via Settings, but the Engine kwarg / EngineOptions accept 0, and
        # this cadence is reused for coverage snapshots even with NO strategist wired
        # (`_maybe_snapshot_coverage`) — `_cadence_due` guards `every <= 0` for that case.
        return cadence_due(n, last, self.strategist_every)

    def _strategy_may(self, strat: dict, key: str) -> bool:
        """One governance verdict shared by Developer preparation and live strategy application."""
        pinned = set(strat.get("_pinned") or [])
        if any(alias in pinned for alias in parallelism_aliases(key)):
            return True
        if key == "card_scoring":
            controls = self._agent_control if isinstance(self._agent_control, dict) else {}
            if self.card_driven_selection and key not in controls:
                return True
        return self._agent_may("strategist", key)

    def _prepare_strategy_developer(self, strat: dict) -> tuple[dict, object, Optional[dict]]:
        """Resolve a Developer transition before its Strategy becomes durable.

        A factory can refuse a live external -> in-process switch because its credential or endpoint
        is unavailable.  Recording the requested backend first made the fold claim the switch had
        happened while the live engine kept its previous Developer; a later resume could then apply
        the same event under different credentials and change treatment halfway through one run.
        Prepare the replacement without mutating the engine, and on refusal record the backend that
        actually remains active plus a bounded, redacted application receipt.
        """
        requested = strat.get("developer")
        current = str(getattr(self, "_developer_name", "default") or "default")
        # A name `validate_strategy` REFUSED, carried here in its own key so it could never be
        # mistaken for a switch. It is a refusal like the four below and gets the same receipt — the
        # difference is only WHERE it was decided, which `reason_code` says. Without this the drop is
        # invisible: the decision keeps its rationale ("switch developer to agentless") with no
        # `developer` and no receipt, i.e. a history that reads as a switch that happened.
        refused_name = strat.get("developer_refused")
        if isinstance(refused_name, str) and refused_name:
            effective = {k: v for k, v in strat.items() if k != "developer_refused"}
            return effective, _NO_PREPARED_DEVELOPER, {
                "status": "refused",
                "requested_backend": refused_name,
                "applied_backend": current,
                "reason_code": "unknown_backend",
                "reason": f"{refused_name!r} is not an available Developer backend",
            }
        if not isinstance(requested, str) or not requested or requested == current:
            return strat, _NO_PREPARED_DEVELOPER, None

        reason_code = ""
        reason = ""
        replacement = _NO_PREPARED_DEVELOPER
        if not self._strategy_may(strat, "developer"):
            reason_code, reason = "governance_locked", "Strategist control of developer is locked"
        elif self.developer_factory is None:
            reason_code, reason = "factory_unavailable", "no Developer replacement factory is wired"
        elif self.unified_agent:
            reason_code, reason = (
                "unified_facade_locked",
                "the unified facade cannot replace only its Developer without breaking its "
                "composed stage contract",
            )
        else:
            try:
                replacement = self.developer_factory(requested)
            except Exception as exc:  # noqa: BLE001 - refusal is durable; the current role remains live
                from looplab.core.redact import redact_persisted_text
                reason_code = type(exc).__name__
                reason = redact_persisted_text(
                    str(exc) or reason_code, max_chars=1_000, single_line=True)

        if replacement is _NO_PREPARED_DEVELOPER:
            effective = {**strat, "developer": current}
            return effective, replacement, {
                "status": "refused",
                "requested_backend": requested,
                "applied_backend": current,
                "reason_code": reason_code,
                "reason": reason,
            }
        return strat, replacement, {
            "status": "applied",
            "requested_backend": requested,
            "applied_backend": requested,
        }

    def _record_strategy(self, strat: dict, state: RunState,
                         ctx: Optional[StrategyContext] = None) -> None:
        # every newly recorded decision has one canonical spelling per concurrency
        # axis. Legacy records remain replayable, but stale aliases cannot survive into the next merge.
        strat = canonicalize_strategy_parallelism(strat)
        strat, prepared_developer, developer_application = self._prepare_strategy_developer(strat)
        ctx_fields = {"phase", "eval_budget_remaining", "failure_rate", "cross_run_receipt"}
        if ctx is not None and ctx.card_driven_selection:
            ctx_fields.add("card_scoring")
        payload = {
            "strategy": strat,
            "at_node": len(state.nodes),
            "ctx": (ctx.model_dump(include=ctx_fields)
                    if ctx is not None else None),
        }
        if developer_application is not None:
            payload["developer_application"] = developer_application
        # THE PAIR THIS DECISION SETS IN ONE BREATH, and until now nothing looked at both. A
        # strategy payload carries `policy` AND `eval_parallel`; a racing schedule (`asha`/`bohb`)
        # cannot fill more than one slot once its seed target is met, because a promotion needs the
        # rung's survivors and an unresolved arm supplies none. `runs/e5small-dr-unified-v4` set
        # `policy: "bohb"` and `eval_parallel: 2` in the SAME event, with a sound argument for the
        # racing schedule and no mention of the width — and then ran one node at a time with a
        # device idle, no card added for 19.5 hours while hypotheses kept arriving.
        #
        # A RECEIPT, NOT A REFUSAL, and deliberately so: the choice can be right, and the engine has
        # no standing to overrule a Strategist on a search-design question it argued for. What it
        # has standing to do is refuse to let the consequence go unrecorded. Additive and
        # fold-ignored; ABSENT when the pair is fine, so every decision that does fill its width
        # writes exactly the payload it always did.
        from looplab.search.policy import policy_fills_width
        _width = strat.get("eval_parallel")
        if not policy_fills_width(strat.get("policy"), _width):
            payload["width_unfilled"] = {
                "policy": str(strat.get("policy") or ""), "eval_parallel": _width,
                "note": ("a racing schedule fills one slot once its seeds are met — a promotion "
                         "needs the rung's survivors, so an unresolved arm blocks both seeding and "
                         "promotion and the remaining slots stay idle until it resolves"),
            }
        self.store.append(EV_STRATEGY_DECISION, payload)
        # The expensive/fallible factory work happened before the append. From here a prepared
        # transition must either install or stop this owner; continuing after the durable event with
        # the old Developer would recreate the fold/live split this receipt exists to prevent.
        self._apply_strategy(
            strat, _prepared_developer=prepared_developer, _strict_developer=True)

    def _ensure_surrogate(self) -> None:
        """Wrap the Researcher in a SurrogateResearcher if it isn't already (idempotent). Used when a
        mid-run strategy switch turns BOHB on: BOHB is ASHA's racing schedule PLUS the surrogate
        proposer, and the proposer is only wired at startup for policy=bohb/surrogate_proposer — so a
        Strategist switching to bohb would otherwise run bare ASHA. Needs numeric bounds; if the
        Researcher (or anything it wraps) exposes none, this is a no-op (bohb degrades to ASHA)."""
        from looplab.search.surrogate import SurrogateResearcher
        # Unified mode: re-wrapping `self.researcher` here would desync it from `self.developer`
        # (the same agent object) — the cli already skips the startup surrogate wrap for the same
        # reason (R1). A mid-run switch to bohb degrades to bare ASHA, which is acceptable.
        if self.unified_agent:
            return
        # "if it isn't already" has to mean the WHOLE wrapper chain, not just the outermost handle.
        # The cli builds the surrogate FIRST and wraps a panel around it, so `researcher_panel > 1`
        # (or foresight) combined with `surrogate_proposer`/`policy=bohb` starts the run as
        # `Panel(Surrogate(base))` — and `_apply_strategy` calls this on EVERY strategy application
        # whose policy is bohb, including a params-only change to a run that was already bohb. An
        # outermost-only isinstance therefore re-wrapped into `Surrogate(Panel(Surrogate(base)))`,
        # demoting the operator's configured panel to the outer surrogate's bootstrap path.
        # The links carry different names — panels hold `.base`, SurrogateResearcher `.fallback`,
        # the roles/unified wrappers `.inner` — so the walk follows all three (`seen` guards the
        # self-referential `inner` unified_agent.py builds).
        chain, seen, link = [], set(), self.researcher
        while link is not None and id(link) not in seen:
            seen.add(id(link))
            chain.append(link)
            link = (getattr(link, "base", None) or getattr(link, "inner", None)
                    or getattr(link, "fallback", None))
        if any(isinstance(r, SurrogateResearcher) for r in chain):
            return
        bounds = next((b for b in (getattr(r, "bounds", None) for r in chain) if b), None)
        if bounds:
            self.researcher = SurrogateResearcher(bounds, fallback=self.researcher,
                                                  explore=self._surrogate_explore)

    def _apply_strategy(
            self, strat: dict, *, _prepared_developer=_NO_PREPARED_DEVELOPER,
            _strict_developer: bool = False) -> None:
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
        # The hidden quality-evidence bootstrap calibrates the complete runtime envelope, not only
        # the policy class, so it must remain immutable.  A public receipt admits that measured launch
        # profile but does not revoke explicit Stage-6 operator controls; those interventions are
        # durable/auditable and make the run ineligible as future calibration evidence.
        if getattr(self, "_speculation_gate_calibration", False) is True:
            return

        # PER-FIELD operator provenance: `_pinned` lists exactly the fields the operator pinned via a
        # `set_strategy` CONTROL_EVENT. Those are EXEMPT from the strategist's grant (the human "can
        # always change it via the UI/snapshot"); every OTHER field — including a strategist-decided
        # field that rides in a record whose top-level source is "operator" — stays gated. Whole-dict
        # `source=="operator"` was too coarse: a later autonomous-consult merge flattened an
        # operator-pinned field's provenance (reverting it on resume), and an operator pin of one field
        # blanket-exempted strategist-decided fields the matrix had locked (mega-review). `_pinned` is
        # not in _strategy_core, so it never affects change-detection; it survives fold as plain data.
        def may(k):
            return self._strategy_may(strat, k)
        if may("novelty_stance") and strat.get("novelty_stance") in NOVELTY_STANCES:
            self._novelty_stance = strat["novelty_stance"]   # Strategist's novelty dial (slice 2)
        card_scoring = validate_card_scoring(strat.get("card_scoring"))
        if card_scoring is not None and may("card_scoring"):
            self._card_scoring = card_scoring
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
        # is read fresh per eval and self._eval_parallel rebuilds the CapacityLimiter each batch, so a
        # mid-run change takes effect on the next node without any rewiring.
        if ("timeout" in strat and may("timeout")
                and not isinstance(strat["timeout"], bool)):
            try:
                _timeout = float(strat["timeout"])
                if math.isfinite(_timeout) and _timeout > 0:
                    self.timeout = max(0.1, _timeout)
            except (TypeError, ValueError, OverflowError):
                pass
        # Layer-2 (docs/23): the Strategist steers the CANONICAL `eval_parallel`/`llm_parallel`; the
        # legacy `max_parallel`/`parallel_build` stay accepted for back-compat and feed the same canonical
        # attributes through the loops below. Live deltas do not retain an AUTO state: 0 settles to
        # safe serial width 1. Startup config alone resolves 0 from hardware / the settled eval width.
        # Legacy alias FIRST, canonical name LAST, so when a strategy carries BOTH the canonical value
        # wins (last write) — matching __init__ ("a NEW name, when set, WINS over its legacy alias").
        for _k in ("max_parallel", "eval_parallel"):
            if _k in strat and may(_k):
                _settled = settle_width(strat[_k], EVAL_WIDTH_MAX)
                if _settled is not None:
                    self._eval_parallel = _settled
        # llm_parallel / parallel_build: concurrent node BUILDS. Rebuilt role pool is lazy
        # (_build_role_pairs), so a mid-run change takes effect on the next draft batch; clamps to 1
        # without a role_factory. Legacy-first / canonical-last, same precedence rule as the eval axis.
        # A live 0 means serial 1; only launch-time 0 couples AUTO build width to settled eval width.
        for _k in ("parallel_build", "llm_parallel"):
            if _k in strat and may(_k):
                _settled = settle_width(strat[_k], LLM_WIDTH_MAX)
                if _settled is not None:
                    self._llm_parallel = _settled
                    if _k == "llm_parallel":
                        # The broker is reconfigured with the SETTLED width, matching what
                        # `_llm_parallel` now holds. Mutate the broker active tasks already
                        # reference: replacing it here would split old/new borrowers across two
                        # independent totals.
                        self._reconfigure_llm_broker(_settled)
        # Per-lane allocation is a distinct canonical Strategist knob. It is deliberately stored raw
        # in `strategy_decision` (including zero), while the live boundary settles every 0 to one
        # worker. Updating the existing broker in place keeps outstanding borrowers under one atomic
        # total+lane controller. Missing lanes are unbounded within the shared total, matching the
        # broker's explicit partial-map contract.
        raw_lanes = strat.get("llm_lane_limits")
        if isinstance(raw_lanes, dict) and may("llm_lane_limits"):
            settled_lanes: dict[str, int] = {}
            lane_values_valid = True
            for lane, value in raw_lanes.items():
                if (lane not in LLM_LANES or isinstance(value, bool)
                        or not isinstance(value, int) or not 0 <= value <= 64):
                    lane_values_valid = False
                    break
                settled_lanes[lane] = max(1, value)
            if lane_values_valid:
                broker = self._llm_broker
                broker.reconfigure(
                    total=broker.snapshot()["total"], lane_limits=settled_lanes)
                self._llm_lane_limits_explicit = True
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
                replacement = (_prepared_developer
                               if _prepared_developer is not _NO_PREPARED_DEVELOPER
                               else self.developer_factory(dev))
                self.developer = replacement
                # cached parallel workers otherwise keep sending implementation calls
                # through the previous backend after the primary Developer has visibly switched.
                self._role_pool = None
                # Layer-5 leases one pair out of that pool. Sessions are joined before the outer
                # Strategist cadence runs, so invalidating the lease here is race-free and ensures the
                # next speculative build observes the newly selected Developer backend.
                self._spec_role_pair = None
                self._pool_developer_override = dev
                # Bind the replacement between calls, before its first implementation request.
                bind_cost_accountants(self)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — live decisions refuse safely; re-entry must be loud
                if _strict_developer:
                    raise

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
        if (not self._coverage_context
                or not self._should_consult(state, marks=state.coverage_snapshots)
                or already_covered_at(state, n, state.coverage_snapshots)):
            return state
        self.store.append(EV_COVERAGE_SNAPSHOT, {
            "at_node": n,
            "projection_token": analytics_projection_token(state),
            **coverage_signal(state, resolution=self.archive_resolution)})
        return fold(self.store.read_all())

    @in_llm_lane("enrichment")
    def _maybe_consult_strategist(self, state: RunState) -> RunState:
        """Operator/boss pin first (HITL parity), then the bounded-cadence Strategist consult.
        Records a `strategy_decision` and re-folds only when the strategy actually changes.

        An operator/boss `set_strategy` pin owns ONLY the fields it names (including canonical
        concurrency totals and the LLM lane allocation); those stay in force for the rest of the run
        (until re-pinned), while the
        autonomous Strategist keeps tuning everything else. The pin is MERGED onto the live strategy
        (not reset to the bare pin) and re-asserted only when a pinned field actually drifts — that,
        plus overlaying the pinned fields onto the Strategist's own decision below, is what stops the
        pin and the Strategist from thrashing (the old "reset to bare pin on any divergence"
        oscillated the policy every consult and dropped the Strategist's fidelity/operators)."""
        if getattr(self, "_speculation_gate_calibration", False) is True:
            return state
        pin = state.pending_strategy or {}
        raw_pin = {k: pin[k] for k in (
            "policy", "policy_params", "fidelity", "eval_parallel", "llm_parallel",
            "llm_lane_limits", "card_scoring")
                   if pin.get(k) is not None}
        n = len(state.nodes)
        consulting = (self.strategist is not None
                      and self._should_consult(state, marks=state.strategy_history)
                      and not self._autonomous_strategy_already_recorded_at(state, n)
                      # THE MONEY BOUND for the in-flight cadence (F1i). Both durable gates above close
                      # only when a decision is RECORDED, and `_record_strategy` records only a strategy
                      # that actually CHANGED — so the ordinary "the Strategist agrees with itself"
                      # outcome leaves the window open. Before F1i that was free: the loop reached this
                      # gate once per node count and then created a node. Since the loop now turns at a
                      # fixed `n` while an evaluation runs, an unchanged decision would buy one paid
                      # consult PER TURN. This memo is in-process only and skips exactly the work whose
                      # outcome was "return state unchanged", so no event, replay or resume can see it;
                      # a resumed engine re-asks once, which is what the durable gates are for.
                      # Keyed on `(n, projection token)` and NOT on `n`, for the reason
                      # `search/coverage.py::already_covered_at` states about its own pair: at a fixed
                      # node count the live inputs still move — a node terminates, a metric lands, an
                      # operator aborts — and each of those is a brief the Strategist has not seen. An
                      # `n`-only memo would silence it for the rest of the node count, which under F1i
                      # is most of a multi-hour run. It still bounds the loop, because the token moves
                      # only when something the brief reads actually changed.
                      and getattr(self, "_strategist_consulted_at", None)
                      != (n, analytics_projection_token(state)))
        active_core = self._strategy_core(state.active_strategy)
        # Cheap pre-check (no ctx/validate): a pin "drifts" if a raw pinned field differs from what's
        # active OR if the currently recorded ownership set differs. Ownership itself is durable
        # behavior: pinning a same-valued field must still exempt it from autonomous control, and a
        # later set_strategy replaces (rather than accumulates) the prior field set. For an INVALID
        # pin this can be a false alarm, so we still validate below before acting on it.
        def pin_matches(k: str, value) -> bool:
            return active_core.get(k) == value

        active_pinned = set((state.active_strategy or {}).get("_pinned") or [])
        pin_drift = bool(raw_pin) and (
            any(not pin_matches(k, v) for k, v in raw_pin.items())
            or set(raw_pin) != active_pinned)
        if not pin_drift and not consulting:
            return state
        # Second short-circuit, for a pin that is drifting only because it is INVALID. `pin_drift` is
        # computed from raw_pin (pre-validation), so a pin carrying only out-of-whitelist fields (a
        # free-text boss `policy`, say) can never match active_core and stays "drifting" for the rest
        # of the run — `pending_strategy` is replaced only by a new set_strategy. validate_strategy
        # then drops it (pin_fields empty), so it is a no-op for RECORDING, but without this memo it
        # was not a no-op for COST: `_strategy_ctx` is O(nodes) in operator_yields and does cross-run
        # memory I/O under a governance lock when cross_run_advisory is on, and it would run on EVERY
        # loop pass rather than on the strategist cadence.
        # The memo is keyed on everything the whitelist can consult for THESE fields: the pin itself,
        # `card_driven_selection` (gates card_scoring) and the policy registry (gates policy). Both
        # can move mid-run, so a pin that only becomes valid later is still picked up. Purely
        # in-process and only ever skips work whose outcome is "return state unchanged", so it cannot
        # affect the event log or resume (a resumed engine simply re-validates once).
        pin_verdict_key = None
        if raw_pin:
            pin_verdict_key = (json.dumps(raw_pin, sort_keys=True, default=str),
                               bool(getattr(self, "card_driven_selection", False)),
                               tuple(available_policies()))
            if not consulting and self._invalid_pin_verdict == pin_verdict_key:
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
        # Latch (or clear) the "this pin survives validation as nothing" verdict the short-circuit
        # above reads. Clearing on a pin that DID survive matters as much as latching: it keeps the
        # memo from outliving the pin that produced it.
        self._invalid_pin_verdict = pin_verdict_key if (raw_pin and not pin_fields) else None
        ownership_drift = bool(pin_fields) and set(pin_fields) != active_pinned
        # 1. Re-assert the pin if a VALID pinned value or its exact ownership set isn't in force
        # (merge onto active). Invalid historical pins remain harmless no-ops.
        if pin_fields and (ownership_drift
                           or any(not pin_matches(k, v) for k, v in pin_fields.items())):
            active = canonicalize_strategy_parallelism(state.active_strategy)
            candidate = {**active, **pin_fields, "source": "operator"}
            strat = validate_strategy(candidate, ctx)
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
            # Spend the in-process attempt memo BEFORE the provider call, never after: an exception
            # from `decide` (or a decision that turns out to change nothing) must still close this
            # node-count for this process, or the very failure mode the memo bounds — one paid consult
            # per outer-loop turn at a fixed `n` — comes back through the error path.
            self._strategist_consulted_at = (n, analytics_projection_token(state))
            with self._op_span("strategist_consult"):
                strat = validate_strategy(self.strategist.decide(state, ctx), ctx)
                if strat:
                    # A legacy-only partial is a NEW delta. Promote it before merging with active
                    # canonical state, or the stale canonical counterpart would shadow this update.
                    strat = canonicalize_strategy_parallelism(strat)
                    strat.update(pin_fields)
                    # Record the decision MERGED onto the live/active strategy (mirror the operator-pin
                    # path above). A strategist decision is a PARTIAL dict — only the fields the model
                    # changed — and `_apply_strategy` never resets an omitted field, so the live engine
                    # ACCUMULATES knobs across consults. Recording the bare partial made fold replace
                    # active_strategy wholesale, so a resumed run reverted every omitted knob (policy,
                    # fidelity, operators, …) to the config default — a silent divergence of the search
                    # machinery from the pre-crash live state (architecture-review M3). `operators` is
                    # applied field-by-field too, so it must DEEP-merge, not replace the whole sub-dict.
                    prev = canonicalize_strategy_parallelism(state.active_strategy)
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
                    # THE REFUSAL RECEIPT SURVIVES THE SECOND PASS, and without this it did not.
                    # `validate_strategy` rebuilds its output from scratch and mints
                    # `developer_refused` only from an INPUT `developer` key — which `merged` no
                    # longer has, because the FIRST pass already dropped the unregistered name. So
                    # the receipt the first pass minted was stripped by the second, and
                    # `_prepare_strategy_developer` saw neither a `developer` nor a
                    # `developer_refused`: the durable `strategy_decision` carried the rationale
                    # ("switch developer to agentless") with no switch and no receipt of any kind,
                    # i.e. exactly the invisible drop the refusal was added to end.
                    #
                    # Restored HERE and taken only from `strat` — this decision's own validated
                    # output — never from `prev`, for `request_research`'s reason three lines up: a
                    # receipt carried forward from `active_strategy` would attribute an earlier
                    # decision's refusal to this one. Not carried inside `validate_strategy` either,
                    # because there the key would become model-settable, and a Strategist claiming a
                    # refusal it never asked for is a false receipt on a durable row.
                    merged.pop("developer_refused", None)
                    if strat.get("developer_refused"):
                        merged["developer_refused"] = strat["developer_refused"]
                    # Carry the CURRENT operator-pinned field set (not the strategist's decision, which
                    # owns no fields) so resume-time _apply_strategy still exempts the operator's knobs
                    # even though this record's top-level source is the strategist's (mega-review).
                    merged["_pinned"] = sorted(pin_fields)
                    if self._strategy_core(merged) != self._strategy_core(state.active_strategy):
                        self._record_strategy(merged, state, ctx)
                        return fold(self.store.read_all())
        return state
