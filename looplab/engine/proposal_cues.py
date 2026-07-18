"""Proposal-time prompt cues (A0d complexity cue + novelty-stance stamp) — extracted from
orchestrator.py as a MIXIN: `class Engine(ProposalCuesMixin, …)` inherits these methods
unchanged, so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves;
these methods SETATTR hint attributes onto the researcher, so this module is part of the
hint-registry discipline: `tests/test_hint_forwarding.py` source-scans it (alongside
orchestrator.py and foresight.py) and asserts every hint attr set here is in
`agents/roles.py::RESEARCHER_HINT_ATTRS`."""
from __future__ import annotations

from looplab.core.models import NodeStatus, RunState
from looplab.trust.cross_run import (cross_run_text, same_live_direction,
                                     sanitize_cross_run_projection, valid_live_direction)

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
        hint += self._cross_run_pointer_text()         # lean "you have cross_run_* tools" nudge (advisory-off default)
        # PART V (B): once the run has a BASE concept set, ask for the DELTA instead of the full list, so
        # per-node annotations stay minimal and inherit down the DAG. Dynamic + gated here (the static
        # system prompt keeps authoring the full set when no base exists — a base-absent run is unchanged).
        if getattr(self, "_concept_run_base", False) and state.run_base_concepts:
            base = ", ".join(str(c) for c in state.run_base_concepts[:40])
            parent_concepts = state.node_concepts.get(parent.id) if parent is not None else None
            parent_line = (f" Your parent experiment's concepts are [{', '.join(str(c) for c in parent_concepts[:40])}]."
                           if parent is not None and parent_concepts else "")
            hint += (
                f"\nConcept authoring — this run has a BASE concept set [{base}]." + parent_line
                + " Do NOT re-list the full set. Author only the CHANGE vs the base + parent: put concepts"
                  " this experiment INTRODUCES in `concepts_added`, and any inherited concept it DROPS"
                  " (e.g. swapping one technology for another) in `concepts_removed`. Leave `concepts`"
                  " empty when you author a delta — the run base and parent inheritance fill the rest.")
        # Concept-slug REUSE (fires for EVERY node incl. node 0, which has no run base yet). A shared slug
        # vocabulary spans ALL runs (the global concept map); an agent inventing `rdrop` when
        # `regularization/r-drop` already exists silently breaks the cross-run prior overlap (exact-slug
        # match). Point it at the fuzzy lookup so consistent slugs emerge at authoring time — cheaper and
        # more robust than post-hoc aliasing. Gated on the tools being wired + concept authoring being on.
        if (getattr(self, "_cross_run_read_tools", False) and getattr(self, "memory_dir", "")
                and (getattr(self, "_concept_pivot", False) or getattr(self, "_concept_run_base", False))):
            hint += ("\nConcept slugs — a shared concept vocabulary spans ALL runs (the global concept map). "
                     "BEFORE minting a concept slug, call find_concept_slugs('<your concept, any spelling>') "
                     "and REUSE the canonical existing slug it returns (matching is separator/case-insensitive "
                     "+ fuzzy, so `rdrop` finds `regularization/r-drop`). Mint a NEW slug only when nothing "
                     "matches — consistent slugs are what let cross-run priors recognise a repeated idea. "
                     "To DECODE a slug (what it is + whether it has helped or hurt across runs) call "
                     "concept_card('<slug>').")
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

    def _cross_run_pointer_text(self) -> str:
        """PART V §22 (advisory): a LEAN, static one-line pointer telling the Researcher it holds the
        cross_run_* READ tools and should consult them before proposing. Closes the default-config gap
        where the tools are wired (cross_run_read_tools ON) but the prompt never NAMES them, so the model
        forgets they exist. Deliberately STATIC — no store I/O on the per-node proposal hot path (that is
        what the tools themselves are for). It fires ALONGSIDE the rich pushed pack (`_cross_run_advisory_text`)
        rather than deferring to it: the pack injects prior-run CONTENT but never names the pull-tools, so
        the two are orthogonal (pushed context vs on-demand drill-down) and the pointer must fire in the
        product default (advisory ON) too, or the tools go permanently unnamed. Gated only on the tools
        being wired + a memory_dir to query. Never touches node selection."""
        if not getattr(self, "_cross_run_read_tools", False) or not getattr(self, "memory_dir", ""):
            return ""
        return ("\nCross-run memory may hold prior attempts and evidence for related runs. Before "
                "proposing, you MAY call cross_run_prior_attempts / cross_run_claims / cross_run_atlas "
                "to check what was already tried and what the evidence supports — advisory only, it "
                "never constrains your choice.")

    def _cross_run_advisory_text(self, state: RunState) -> str:
        """§21.20 Step 5 (advisory): the bounded cross-run CONTEXT PACK for the Researcher prompt —
        evidence-grounded claims with BOTH support and counter-evidence (Step 4) plus a bounded live concept-
        observation line (Step 3), rendered as a short prose block. Folded into the prompt hint like the E4
        prior note; advisory only, NEVER touches node selection (§21.7). Off unless `cross_run_advisory`;
        returns "" on no memory dir / empty store / any hiccup, so the prompt is byte-identical when off."""
        if not getattr(self, "_cross_run_advisory", False) or not getattr(self, "memory_dir", ""):
            self._cross_run_advisory_receipt = {}
            return ""
        current_direction = getattr(state, "direction", None)
        # CODEX AGENT: this text enters the Researcher prompt. An invalid current direction cannot
        # safely interpret any historical outcome, even when a legacy row has the same task id.
        if not valid_live_direction(current_direction):
            self._cross_run_advisory_receipt = {}
            return ""
        try:
            import hashlib
            import json
            from pathlib import Path

            from looplab.engine.claims import (build_context_pack, claims_for_memory, load_research_claims,
                                               render_context_pack)
            from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
            from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
            from looplab.events.eventstore import read_jsonl_lenient
            base = Path(self.memory_dir)
            lp, cp = base / "lessons.jsonl", base / "concept_capsules.jsonl"
            lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if lp.exists() else []
            capsules = ConceptCapsuleStore(cp).all() if cp.exists() else []
            # Freeze one task-scoped view for this prompt. Exact task id is authoritative only after
            # direction provenance matches; related-task transfer uses the same fingerprint threshold as
            # lesson priors and never includes this run.
            from looplab.engine.memory import fingerprint_similarity
            rid, tid = str(state.run_id or ""), str(state.task_id or "")
            fp_fn = getattr(self, "_task_fingerprint", None)
            fp = ([t for t in fp_fn(state, state.best()) if not str(t).startswith("param:")]
                  if callable(fp_fn) else [])

            def _scoped(row):
                if rid and str(row.get("run_id") or "") == rid:
                    return False
                # Direction is a hard semantic boundary even for an exact task id: support for a
                # minimisation objective can mean the opposite thing when that id is later reused for
                # maximisation. Legacy/garbled rows remain available to audit views, not live prompts.
                if not same_live_direction(current_direction, row.get("direction")):
                    return False
                # This method always builds live agent context. With neither a stable task id nor a
                # bounded task fingerprint there is no defensible scope, so a same-polarity portfolio
                # row still fails closed. Portfolio-wide inspection belongs to explicit audit tools.
                if not tid and not fp:
                    return False
                if tid and str(row.get("task_id") or "") == tid:
                    return True
                stored = row.get("fingerprint")
                if not isinstance(stored, list):
                    return False
                stored = [t for t in stored if not str(t).startswith("param:")]
                return fingerprint_similarity(fp, stored) >= 0.34

            lessons = [r for r in lessons if _scoped(r)]
            capsules = [r for r in capsules if _scoped(r)]
            research = [r for r in load_research_claims(base)
                        if (not rid or str(r.get("run_id") or "") != rid)
                        and bool(tid) and str(r.get("task_id") or "") == tid
                        and same_live_direction(current_direction, r.get("direction"))]
            # Resolve the SAME taxonomy snapshot as the Atlas (aliases + splits), so a purged/merged/split
            # concept never leaks into the proactive prompt through this raw overview (CODEX).
            overview = (portfolio_concept_overview(capsules,
                        aliases=load_concept_aliases(base), splits=load_concept_splits(base))
                        if capsules else None)
            if not lessons and not overview and not research:
                self._cross_run_advisory_receipt = {}
                return ""
            # lessons + D8 claims + operator decisions; structured claim key when enabled (§21.20.13).
            claims = claims_for_memory(base, lessons=lessons, research_claims=research,
                                       structured=getattr(self, "_cross_run_structured_claims", False))
            pack = build_context_pack(claims, concept_overview=overview)
            text = render_context_pack(pack)
            text = cross_run_text(text, max_chars=16_000, single_line=False, entropy=True)
            # Digest the exact bounded structured pack behind the rendered prompt, not raw legacy stores.
            # A raw hash is both a credential oracle and an identity for bytes the model never received.
            corpus_projection = sanitize_cross_run_projection(
                pack, max_chars=64_000, max_items=64, max_total_items=2_048)
            corpus = json.dumps(corpus_projection,
                                ensure_ascii=False, sort_keys=True, default=str,
                                separators=(",", ":")).encode("utf-8")
            self._cross_run_advisory_receipt = {
                "v": 1,
                "scope_task": cross_run_text(
                    tid, max_chars=500, single_line=True, entropy=False),
                "excluded_run": cross_run_text(
                    rid, max_chars=500, single_line=True, entropy=False),
                "n_lessons": len(lessons), "n_capsules": len(capsules), "n_research": len(research),
                "corpus_digest": hashlib.sha256(corpus).hexdigest(),
                "render_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            return ("\n" + text) if text else ""
        except Exception:  # noqa: BLE001 — advisory context is best-effort, never blocks proposing
            self._cross_run_advisory_receipt = {}
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
