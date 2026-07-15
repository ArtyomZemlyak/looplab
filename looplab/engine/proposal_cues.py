"""Proposal-time prompt cues (A0d complexity cue + novelty-stance stamp) — extracted from
orchestrator.py as a MIXIN: `class Engine(ProposalCuesMixin, …)` inherits these methods
unchanged, so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves;
these methods SETATTR hint attributes onto the researcher, so this module is part of the
hint-registry discipline: `tests/test_hint_forwarding.py` source-scans it (alongside
orchestrator.py and foresight.py) and asserts every hint attr set here is in
`agents/roles.py::RESEARCHER_HINT_ATTRS`."""
from __future__ import annotations

from looplab.core.models import NodeStatus, RunState

# PART IV Phase 2b: the streak length at which the capability-expansion directive treats the run as
# action-space LOCKED-IN. Matches `search/lock_in.py::lock_in_signal`'s default `streak_threshold` (the
# 2a concept snapshot records the raw streak LENGTH, so the fire test lives here). Kept in sync with it.
_LOCK_IN_STREAK = 5


class ProposalCuesMixin:
    """The engine's proposal-cue cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _set_complexity_hint(self, state: RunState, parent) -> None:
        """Inject the engine-computed proposal cues into the next prompt: A0d (breadth-keyed
        complexity) + A5 (remaining eval budget). No-op unless the respective knob is on; harmless on
        Toy roles. Both flow via the single `_complexity_hint` attribute both Researchers read."""
        hint = ""
        if self._complexity_cue:
            nc = (sum(1 for n in state.nodes.values() if parent.id in n.parent_ids)
                  if parent is not None else len([n for n in state.nodes.values() if not n.parent_ids]))
            level = ("a minimal baseline" if nc < 2 else "a moderate approach" if nc < 4
                     else "an advanced approach (ensembling / HPO / feature-engineering)")
            hint += (f"\nComplexity guidance: this branch already has {nc} sibling experiment(s); "
                     f"propose {level}.")
        if self._budget_aware:
            max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
            if max_es:
                rem = max(0.0, max_es - state.total_eval_seconds)
                frac = rem / max_es if max_es else 1.0
                stance = ("explore broadly — plenty of budget" if frac > 0.5 else
                          "be selective — budget is over half spent" if frac > 0.2 else
                          "exploit the leader with cheap experiments — budget nearly spent")
                hint += (f"\nBudget guidance: {rem:.0f}s of {max_es:.0f}s eval budget remain "
                         f"({frac:.0%}); {stance}.")
        if self._failure_reflection:
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error_reason),
                           key=lambda n: n.id, reverse=True)[:3]
            if fails:
                def _why(n) -> str:
                    # Signal-delivery (§1): prefer the crash-triage VERDICT (the LLM's judgment of
                    # why the idea/code failed) over the raw stderr tail — that judgment is the most
                    # expensive reasoning in the failure path and was previously dropped by the fold.
                    tr = " ".join((getattr(n, "triage_rationale", "") or "").split())[:90]
                    return tr or (n.error or "")[:60]
                summ = "; ".join(f"node {n.id} ({n.error_reason}): {_why(n)}" for n in fails)
                hint += f"\nReflection — recent failures to avoid repeating: {summ}."
        # Signal-delivery (§1): surface a recently trust-FLAGGED node so the next proposal reacts to
        # it (trust flags otherwise only bar a WIN — the agent never learns and keeps re-deriving the
        # flagged approach). Pure rendering lives in digest.trust_reflection so a test can exercise it.
        from looplab.events.digest import trust_reflection
        hint += trust_reflection(state)
        if self._localize_faults and self._repo_spec.get("editables"):
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error),
                           key=lambda n: n.id, reverse=True)
            if fails:
                from looplab.engine.localize import localize
                roots = [e["path"] for e in self._repo_spec["editables"]]
                loc = localize(fails[0].error, roots,
                               idea_text=(parent.idea.rationale if parent is not None else ""))
                if loc:
                    files = ", ".join(item["file"] for item in loc[:3])
                    hint += f"\nFault localization — likely files to edit: {files}."
        if self._feature_engineering and (self.task_has_columns or self._assets):
            hint += ("\nFeature engineering: propose 1-2 semantically-meaningful engineered features "
                     "(ratios, interactions, aggregations, domain transforms) as code. The eval's "
                     "cross-validation gates them — KEEP a feature only if it improves CV; drop any "
                     "that don't (feature engineering is non-universal).")
        hint += self._prior_note_text   # E4: cross-run meta-learned prior (empty unless enabled)
        hint += self._cross_run_advisory_text(state)   # §21.20 Step 5: cross-run context pack (empty unless enabled)
        try:
            setattr(self.researcher, "_complexity_hint", hint)
        except Exception:  # noqa: BLE001
            pass
        # A7 `prefer_sweep`: nudge — never force — the Researcher toward an intra-node sweep when the
        # Strategist's cost model favors in-process execution. Cleared when the flag is off, so a one-
        # time bias doesn't persist after the Strategist moves on.
        sweep_hint = ("\nStrategy bias: evals here are costly and the space is numeric — STRONGLY "
                      "consider a SWEEP (set `space` to a small grid) so many configs share one "
                      "data load." if self._prefer_sweep else "")
        try:
            setattr(self.researcher, "_sweep_hint", sweep_hint)
        except Exception:  # noqa: BLE001
            pass
        self._stamp_novelty_hint(state, self._novelty_stance)

    def _cross_run_advisory_text(self, state: RunState) -> str:
        """§21.20 Step 5 (advisory): the bounded cross-run CONTEXT PACK for the Researcher prompt —
        evidence-grounded claims with BOTH support and counter-evidence (Step 4) plus a portfolio-coverage
        line (Step 3), rendered as a short prose block. Folded into the prompt hint EXACTLY like the E4
        prior note; advisory only, NEVER touches node selection (§21.7). Off unless `cross_run_advisory`;
        returns "" on no memory dir / empty store / any hiccup, so the prompt is byte-identical when off."""
        if not getattr(self, "_cross_run_advisory", False) or not getattr(self, "memory_dir", ""):
            return ""
        try:
            import json
            from pathlib import Path

            from looplab.engine.claims import build_context_pack, claims_for_memory, render_context_pack
            from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
            from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
            from looplab.events.eventstore import read_jsonl_lenient
            base = Path(self.memory_dir)
            # CODEX AGENT: `state` is never used below: this live prompt reads the whole mutable portfolio,
            # including current-run/mismatched-task rows, on every proposal. Nothing records the store
            # revision, selected evidence, rendered bytes, or prompt digest before the LLM call, so a crash
            # + resume can re-propose the same slot under different evidence. Scope through one immutable
            # snapshot and append a retrieval receipt before any cross-run content can influence reasoning.
            lp, cp = base / "lessons.jsonl", base / "concept_capsules.jsonl"
            lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if lp.exists() else []
            # Resolve the SAME taxonomy snapshot as the Atlas (aliases + splits), so a purged/merged/split
            # concept never leaks into the proactive prompt through this raw overview (CODEX).
            overview = (portfolio_concept_overview(ConceptCapsuleStore(cp).all(),
                        aliases=load_concept_aliases(base), splits=load_concept_splits(base))
                        if cp.exists() else None)
            # CODEX AGENT: `research_claims.jsonl` is a first-class source, but this early return checks only
            # lessons/capsules. D8-only memory is visible through the API helper yet silently absent from the
            # Researcher prompt. Centralize source loading/readiness in `claims_for_memory` instead.
            if not lessons and not overview:
                return ""
            # lessons + D8 claims + operator decisions; structured claim key when enabled (§21.20.13).
            claims = claims_for_memory(base, lessons=lessons,
                                       structured=getattr(self, "_cross_run_structured_claims", False))
            text = render_context_pack(build_context_pack(claims, concept_overview=overview))
            return ("\n" + text) if text else ""
        except Exception:  # noqa: BLE001 — advisory context is best-effort, never blocks proposing
            return ""

    def _stamp_novelty_hint(self, state: RunState, stance: str) -> None:
        """Stamp the Strategist's novelty dial onto the ACTIVE researcher (slice 2/4): a prose
        directive `_novelty_hint` (+ the coverage gaps to act on) that the researcher folds into its
        prompt, plus the stance VALUE `_novelty_stance` the foresight ranker reads. "balanced" ->
        empty hint (byte-identical to today's prompt). Extracted so the DEBUG/repair path can force a
        NEUTRAL "balanced" stance — novelty pressure ("open a new direction") is wrong when the job is
        to FIX a failure — and so draft/improve refresh it from the live `self._novelty_stance` every
        node (no stale hint bleeds from a prior operator into a later one)."""
        nov_hint = ""
        if stance == "explore":
            # Reuse the breadth snapshot the strategist cadence already recorded (its most recent view)
            # instead of recomputing the O(nodes) signal on this per-proposal hot path — the hint is
            # prose, so the last snapshot is fresh enough. Falls back to {} before the first snapshot.
            cov = state.coverage_snapshots[-1] if state.coverage_snapshots else {}
            top = cov.get("top_themes") or []
            spread = (f" So far the search concentrates on '{top[0][0]}' "
                      f"({cov.get('dominant_theme_frac', 0.0):.0%} of experiments); "
                      f"themes tried: {[t for t, _ in top]}." if top else "")
            nov_hint = "\nNovelty stance: EXPLORE — the search is narrowing." + spread
            # PART IV Phase 2a: when the concept-graph pivot is on and its cadence recorded an
            # uncovered-region alarm, name the SPECIFIC regions ("0 coverage in {X} — go there") instead
            # of the vague "broaden" — a far more actionable directive (§21.11). Falls back to the generic
            # broaden directive when the pivot is off or no region is uncovered.
            pivot = ""
            if getattr(self, "_concept_pivot", False):
                csnaps = state.concept_coverage_snapshots
                cs = csnaps[-1] if csnaps else {}
                if cs.get("fired") and cs.get("directive"):
                    pivot = ("\nConcept-graph pivot — " + cs["directive"] +
                             " Propose an experiment in one of those uncovered regions.")
            nov_hint += pivot or (
                " Propose a MEANINGFULLY DIFFERENT direction (a new theme / approach / component), not "
                "a variation of the current leader — broaden the space.")
            # PART IV Phase 2b (D7, §21.8, issue #7): when the capability-expansion lever is on and the
            # concept-graph cadence detected action-space LOCK-IN (a long consecutive same-lever streak),
            # ESCALATE past "broaden" to a forced-JUMP directive — expand the action space / build the
            # missing infra, do NOT swap another variant of the saturated lever (the node_63/rubertlite
            # failure: 12 consecutive loss-only experiments while the metric plateaued). This is the
            # PROMPT half; the SCORED half now ships too (§21.13) — orchestrator stamps this proposal's
            # operator `expand` on the SAME `capability_expansion_due` gate, so operator_yields measures
            # whether it paid off. Reads the 2a snapshot, so it no-ops without `concept_pivot`.
            if getattr(self, "_capability_expansion", False):
                # Gate on the CURRENT streak (clears after a successful pivot), name the CURRENTLY-locked
                # axis — via the shared `capability_expansion_due` helper the D7 operator stamp also uses,
                # so the prose directive and the `expand` operator fire on EXACTLY the same condition.
                from looplab.search.lock_in import capability_expansion_due
                due, axis, streak = capability_expansion_due(state, streak_threshold=_LOCK_IN_STREAK)
                if due:
                    nov_hint += (
                        f"\nCapability expansion — the search is still confined to ONE subsystem "
                        f"('{axis}'): {streak} consecutive experiments there (action-space lock-in). Do "
                        f"NOT propose another variant of the '{axis}' lever. EXPAND THE ACTION SPACE: "
                        # Task-AGNOSTIC categories (no domain-specific prescription): the concrete build
                        # is the researcher's to derive from THIS task's assets/uncovered regions.
                        "build a capability the run has never had — new data / inputs, a different model "
                        "or representation, or a different evaluation — that reaches a region the current "
                        "lever can't. You have full file freedom; a genuinely new capability beats another "
                        "tweak of the saturated one.")
        elif stance == "exploit":
            nov_hint = ("\nNovelty stance: EXPLOIT — refine and deepen the current best line of "
                        "attack; a focused improvement beats opening a new direction now.")
        for _attr, _val in (("_novelty_hint", nov_hint), ("_novelty_stance", stance)):
            try:
                setattr(self.researcher, _attr, _val)
            except Exception:  # noqa: BLE001
                pass
