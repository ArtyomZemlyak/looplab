"""Proposal-time prompt cues (A0d complexity cue + novelty-stance stamp) — extracted from
orchestrator.py as a MIXIN: `class Engine(ProposalCuesMixin, …)` inherits these methods
unchanged, so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves;
these methods SETATTR hint attributes onto the researcher, so this module is part of the
hint-registry discipline: `tests/test_hint_forwarding.py` source-scans it (alongside
orchestrator.py and foresight.py) and asserts every hint attr set here is in
`agents/roles.py::RESEARCHER_HINT_ATTRS`."""
from __future__ import annotations

import math

from looplab.core.models import NodeStatus, RunState, normalize_steering_context
from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.search.coverage import latest_live_snapshot
from looplab.trust.cross_run import (cross_run_text, same_live_direction,
                                     sanitize_cross_run_projection, valid_live_direction)

# PART IV Phase 2b: the streak length at which the capability-expansion directive treats the run as
# action-space LOCKED-IN. Matches `search/lock_in.py::lock_in_signal`'s default `streak_threshold` (the
# 2a concept snapshot records the raw streak LENGTH, so the fire test lives here). Kept in sync with it.
_LOCK_IN_STREAK = 5


class ProposalCuesMixin:
    """The engine's proposal-cue cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _experiment_time_budget(self):
        """The operative per-experiment wall-clock CEILING (seconds) a training must finish within,
        resolved the SAME way the eval dispatcher resolves it — NOT the config `timeout` (default 30s,
        the solution.py path; config.py:333 states 'RepoTasks use their per-profile timeout'). For a repo
        task with an ACTIVATED eval_spec the ceiling is the LARGEST profile/base timeout
        `command_eval.build_command` could apply (a node runs under whichever profile it selects, so the
        largest is the real ceiling a training must fit); when no eval_spec is active the solution.py
        `self.timeout` genuinely stands (eval_dispatch's else-branch). Returns None when no finite, positive
        budget is knowable, so the cue degrades to generic wording instead of surfacing a wrong number.

        Fixes the pre-fix cue, which read `self.timeout` unconditionally and printed '~30s (~0.0h)' for a
        multi-hour repo training whose real per-profile budget was 600s-18000s — the exact opposite of what
        the same cue then tells the Researcher to size against."""
        from looplab.runtime.sandbox import finite_timeout
        es = getattr(self, "_eval_spec", None) or {}
        if es:
            vals = []
            base = es.get("timeout")
            if base is not None:
                vals.append(base)
            for prof in (es.get("profiles") or {}).values():
                if isinstance(prof, dict) and "timeout" in prof:
                    vals.append(prof["timeout"])
            if not vals:                    # eval_spec active but no explicit budget -> build_command's default
                vals.append(600.0)
            cand = max((finite_timeout(v, 0.0) for v in vals), default=0.0)
        else:
            cand = self.timeout if isinstance(self.timeout, (int, float)) else 0.0
        return cand if isinstance(cand, (int, float)) and math.isfinite(cand) and cand > 0 else None

    def _set_complexity_hint(self, state: RunState, parent, researcher=None) -> None:
        """Inject the engine-computed proposal cues into the next prompt: A0d (breadth-keyed
        complexity) + A5 (remaining eval budget). No-op unless the respective knob is on; harmless on
        Toy roles. Both flow via the single `_complexity_hint` attribute both Researchers read.
        `researcher` (Variant-1): stamp the cues onto THIS build's own researcher instance (a pool member)
        instead of the shared `self.researcher`, so concurrent builds don't clobber each other's hints."""
        _r = researcher if researcher is not None else self.researcher
        hint = ""
        steering: list[dict] = []
        if self._complexity_cue:
            nc = (sum(1 for n in state.nodes.values() if parent.id in n.parent_ids)
                  if parent is not None else len([n for n in state.nodes.values() if not n.parent_ids]))
            level_key = "minimal" if nc < 2 else "moderate" if nc < 4 else "advanced"
            level = ("a minimal baseline" if level_key == "minimal" else "a moderate approach"
                     if level_key == "moderate"
                     else "an advanced approach (ensembling / HPO / feature-engineering)")
            hint += (f"\nComplexity guidance: this branch already has {nc} sibling experiment(s); "
                     f"propose {level}.")
            steering.append({"kind": "complexity", "siblings": min(nc, 1_000_000),
                             "level": level_key})
        if self._budget_aware:
            max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
            if max_es:
                rem = max(0.0, max_es - state.total_eval_seconds)
                frac = rem / max_es if max_es else 1.0
                stance_key = "explore" if frac > 0.5 else "selective" if frac > 0.2 else "exploit"
                stance = ("explore broadly — plenty of budget" if stance_key == "explore" else
                          "be selective — budget is over half spent" if stance_key == "selective" else
                          "exploit the leader with cheap experiments — budget nearly spent")
                hint += (f"\nBudget guidance: {rem:.0f}s of {max_es:.0f}s eval budget remain "
                         f"({frac:.0%}); {stance}.")
                if (isinstance(max_es, (int, float)) and not isinstance(max_es, bool)
                        and math.isfinite(float(max_es)) and 0 < float(max_es) <= 1e12):
                    steering.append({"kind": "eval_budget", "remaining_seconds": rem,
                                     "total_seconds": float(max_es), "stance": stance_key})
        # Experiment TIME-BUDGET cue (repo tasks): a training that cannot finish inside the per-experiment
        # wall-clock limit is KILLED and yields NO metric — pure waste. Real runs configured 26h/7h
        # trainings against a ~5h limit and timed out repeatedly because no role SAW the limit or estimated
        # fit. Surface the operative limit + prior nodes' MEASURED eval wall-clock (fit vs killed) so the
        # Researcher sizes epochs/steps to fit and probes per-step time when it's unknown.
        if self._repo_spec:
            timed = sorted((n for n in state.nodes.values()
                            if isinstance(getattr(n, "eval_seconds", None), (int, float))
                            and n.eval_seconds and n.eval_seconds > 0),
                           key=lambda n: n.id, reverse=True)[:3]

            def _outcome(n) -> str:
                # A completed node's time is a real fit measurement; a TIMED-OUT node hit the ceiling (the
                # one signal to size smaller); a node that failed for another reason (crash/oom/setup) ran
                # that long then died for a NON-time reason, so labelling it "killed" would misteach the
                # Researcher to shrink a training that actually crashed. Use the fold's own error_reason.
                if n.status is not NodeStatus.failed:
                    return " (completed)"
                reason = getattr(n, "error_reason", None)
                if reason == "timeout":
                    return " — TIMED OUT (exceeded budget)"
                return f" — failed ({reason})" if reason else " — failed"

            calib = "; ".join(f"node {n.id}: {n.eval_seconds / 60:.0f} min" + _outcome(n) for n in timed)
            limit = self._experiment_time_budget()
            limit_txt = (f"each experiment (train+eval) must finish within ~{limit:.0f}s "
                         f"(~{limit / 3600.0:.1f}h)" if limit else
                         "each experiment runs under a fixed wall-clock budget")
            hint += (
                f"\nExperiment TIME BUDGET — {limit_txt}. A training that exceeds it is KILLED and yields "
                f"NO metric (pure waste). BEFORE fixing epochs/steps, ESTIMATE the wall-clock: "
                f"total_steps = epochs × ceil(train_rows / batch_size); total_steps × per-step-time must "
                f"stay WELL under the budget (leave room for data prep + eval). If per-step time on THIS "
                f"data/hardware is unknown, run a SHORT probe (a few hundred steps or a subsample) to "
                f"measure it FIRST, then size epochs to fit — a smaller experiment that COMPLETES beats a "
                f"bigger one that gets killed."
                + (f" Measured so far — {calib}." if calib else ""))
            if limit is not None:
                steering.append({"kind": "experiment_time_budget", "seconds": limit})
        # Layer-4 resource cue: the Researcher declares a GPU count and the scheduler exposes that
        # many devices. This replaces the old unconditional single-device advice while retaining the
        # documented legacy behavior when the declaration is omitted.
        if self._repo_spec and getattr(self, "_gpu_ids", None):
            pool = len(self._gpu_ids)
            legacy = ("one device in parallel mode" if self._eval_parallel > 1
                      else "the whole visible box in serial mode")
            hint += (
                f"\nGPU RESOURCE CONTRACT — this pool exposes at most {pool} GPU(s). Set "
                "`footprint.gpus` to the exact count this experiment needs (0 means CPU-only); its "
                "training/eval command must target that SAME count. The scheduler clamps impossible "
                "requests and exposes only the reserved devices through CUDA_VISIBLE_DEVICES. Do not "
                "copy a repo README's `--gpus 2`/`--gpus 4` unless the footprint declares it. Leaving "
                f"the footprint unspecified preserves legacy behavior: {legacy}.")
            steering.append({"kind": "gpu_constraint", "mode": "declared_footprint"})
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
                steering.append({"kind": "failure_reflection",
                                 "node_ids": [n.id for n in fails]})
        # Signal-delivery (§1): surface the live-watchdog observations (train-monitor health verdicts +
        # ASHA intermediate-rank flags) so the next proposal reacts to a config whose TRAINING was seen
        # to be weak — even when the watchdog kills are OFF (the default) and the node ran to completion,
        # so its live curve would otherwise be lost (those diagnostics are fold-ignored, invisible to
        # the failure-reflection above). Reads the raw event rows (bounded/deduped inside the helper).
        if self._watchdog_reflection:
            from looplab.events.digest import watchdog_reflection
            watchdog_hint = watchdog_reflection(self.store.read_all(), state=state)
            hint += watchdog_hint
            if watchdog_hint:
                steering.append({"kind": "watchdog_reflection"})
        # Signal-delivery (§1): surface a recently trust-FLAGGED node so the next proposal reacts to
        # it (trust flags otherwise only bar a WIN — the agent never learns and keeps re-deriving the
        # flagged approach). Pure rendering lives in digest.trust_reflection so a test can exercise it.
        from looplab.events.digest import trust_reflection
        trust_hint = trust_reflection(state)
        hint += trust_hint
        if trust_hint:
            steering.append({"kind": "trust_reflection"})
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
                    steering.append({"kind": "fault_localization",
                                     "file_count": min(len(loc), 1_000_000)})
        if self._feature_engineering and (self.task_has_columns or self._assets):
            hint += ("\nFeature engineering: propose 1-2 semantically-meaningful engineered features "
                     "(ratios, interactions, aggregations, domain transforms) as code. The eval's "
                     "cross-validation gates them — KEEP a feature only if it improves CV; drop any "
                     "that don't (feature engineering is non-universal).")
            steering.append({"kind": "feature_engineering"})
        prior_hint = self._prior_note_text
        hint += prior_hint   # E4: cross-run meta-learned prior (empty unless enabled)
        if prior_hint:
            steering.append({"kind": "reflection_prior"})
        # Deep-research prose/findings remain on the research timeline.  The Card records only which
        # exact memo was active when this proposal was formed, so future delivery can drill down without
        # copying model-authored text (or paths/source bodies) into the tokenless public Card dump.
        if state.research:
            from looplab.core.advisory_payloads import valid_advisory_ref
            latest_memo = state.research[-1]
            memo_id = latest_memo.get("memo_id") if isinstance(latest_memo, dict) else None
            if valid_advisory_ref(memo_id, "memo"):
                steering.append({"kind": "research_memo", "ref": memo_id})
        # §21.20 Step 5: cross-run context pack (empty unless enabled). `_cross_run_advisory_text`
        # sets `self._cross_run_advisory_receipt` as a side effect; Variant-1: hold `_advisory_lock`
        # across the compute + the capture, then stamp the receipt onto THIS build's researcher so a
        # concurrent sibling draft can't mis-attribute its provenance to this node. The lock is
        # uncontended (and the block a no-op) on the serial path / when advisory is off.
        _adv_lock = getattr(self, "_advisory_lock", None)
        if _adv_lock is not None:
            with _adv_lock:
                hint += self._cross_run_advisory_text(state)
                _receipt = getattr(self, "_cross_run_advisory_receipt", {})
        else:  # bare test hosts without the engine __init__ (no lock) — original behaviour
            hint += self._cross_run_advisory_text(state)
            _receipt = getattr(self, "_cross_run_advisory_receipt", {})
        try:
            setattr(_r, "_cross_run_advisory_receipt", _receipt)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(_receipt, dict) and _receipt:
            digest = _receipt.get("corpus_digest")
            if (isinstance(digest, str) and len(digest) == 64
                    and all(ch in "0123456789abcdef" for ch in digest)):
                steering.append({"kind": "cross_run_advisory", "ref": f"sha256:{digest}",
                                 "status": "available"})
            elif _receipt.get("status") == "unavailable":
                steering.append({"kind": "cross_run_advisory", "status": "unavailable"})
        pointer_hint = self._cross_run_pointer_text()
        hint += pointer_hint         # lean "you have cross_run_* tools" nudge (advisory-off default)
        if pointer_hint:
            steering.append({"kind": "cross_run_tools"})
        # PART V (B): once the run has a BASE concept set, ask for the DELTA instead of the full list, so
        # per-node annotations stay minimal and inherit down the DAG. Dynamic + gated here (the static
        # system prompt keeps authoring the full set when no base exists — a base-absent run is unchanged).
        if getattr(self, "_concept_run_base", False) and state.run_base_concepts:
            # CODEX AGENT: unresolved inheritance must force full authoring; fallback [] never enables delta.
            from looplab.search.concept_projection import (bounded_untrusted_concept_json,
                                                            concept_inheritance_context)
            concept_context = concept_inheritance_context(
                state, parent.id if parent is not None else None)
            hint += ("\nUNTRUSTED_RECORDED_CONCEPT_DATA="
                     + bounded_untrusted_concept_json(concept_context))
            if concept_context["delta_safe"]:
                hint += (
                    "\nConcept authoring — delta mode is enabled for a root/draft or this exact primary "
                    "parent. Set `concept_mode=\"delta\"`; do NOT re-list the full set. Author only the "
                    "CHANGE in `concepts_added` and `concepts_removed`; leave `concepts` empty; both delta "
                    "lists may be empty to inherit unchanged. If you propose operator=merge, use "
                    "`concept_mode=\"full\"` because the other actual parent memberships are not supplied "
                    "in this prompt.")
                steering.append({"kind": "concept_authoring", "mode": "delta"})
            else:
                hint += (
                    "\nConcept authoring safety — inherited membership is UNAVAILABLE or PARTIAL. "
                    "You MUST set `concept_mode=\"full\"`, put the exact complete concept set in `concepts`, "
                    "leave both delta lists empty, and MUST NOT use delta mode for this proposal.")
                steering.append({"kind": "concept_authoring", "mode": "full"})
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
                     "To DECODE a slug (what it is + where it ranked within comparable prior runs) call "
                     "concept_card('<slug>').")
            steering.append({"kind": "concept_slug_reuse"})
        try:
            setattr(_r, "_complexity_hint", hint)
        except Exception:  # noqa: BLE001
            pass
        # A7 `prefer_sweep`: nudge — never force — the Researcher toward an intra-node sweep when the
        # Strategist's cost model favors in-process execution. Cleared when the flag is off, so a one-
        # time bias doesn't persist after the Strategist moves on.
        sweep_hint = ("\nStrategy bias: evals here are costly and the space is numeric — STRONGLY "
                      "consider a SWEEP (set `space` to a small grid) so many configs share one "
                      "data load." if self._prefer_sweep else "")
        try:
            setattr(_r, "_sweep_hint", sweep_hint)
        except Exception:  # noqa: BLE001
            pass
        if sweep_hint:
            steering.append({"kind": "sweep"})
        self._stamp_novelty_hint(state, self._novelty_stance, researcher=_r)
        strategy_cue = {"kind": "strategy"}
        if self._novelty_stance in {"explore", "balanced", "exploit"}:
            strategy_cue["novelty_stance"] = self._novelty_stance
        fidelity = getattr(self, "_strategy_fidelity", None)
        if fidelity in {"cheap", "balanced", "full"}:
            strategy_cue["fidelity"] = fidelity
        if len(strategy_cue) > 1:
            steering.append(strategy_cue)
        bounded_steering = normalize_steering_context(steering)
        try:
            setattr(_r, "_steering_context", bounded_steering or [])
        except Exception:  # noqa: BLE001
            pass

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

    def _cross_run_advisory_text(
            self, state: RunState, *, _governance: dict | None = None) -> str:
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

            from looplab.engine.claims import (
                _filter_claim_source_rows,
                build_context_pack,
                claims_for_memory,
                load_claim_lessons,
                load_research_claims,
                render_context_pack,
            )
            from looplab.engine.governance_health import project_governed_sources
            from looplab.engine.memory import (
                ConceptCapsuleStore,
                _capsule_source_summary,
                _capsule_completeness,
                _capsule_fingerprint_scope_complete,
                _portfolio_concept_overview_data,
                _filter_capsule_rows,
            )
            base = Path(self.memory_dir)
            if _governance is None:
                return project_governed_sources(
                    base,
                    lambda governance: self._cross_run_advisory_text(
                        state, _governance=governance),
                    include_concepts=True,
                    source_names=(
                        "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl"),
                )
            cp = base / "concept_capsules.jsonl"
            lessons = load_claim_lessons(base)
            from looplab.engine.governance_health import observed_path_missing
            capsules = ConceptCapsuleStore(cp).all() if not observed_path_missing(cp) else []
            # Freeze one task-scoped view for this prompt. Exact task id is authoritative only after
            # direction provenance matches; related-task transfer uses the same fingerprint threshold as
            # lesson priors and never includes this run.
            from looplab.engine.memory import fingerprint_similarity
            rid, tid = str(state.run_id or ""), str(state.task_id or "")
            fp_fn = getattr(self, "_task_fingerprint", None)
            fp = ([t for t in fp_fn(state, state.best()) if not str(t).startswith("param:")]
                  if callable(fp_fn) else [])

            scope_unknown = fingerprint_unknown = fingerprint_omitted = direction_unknown = 0
            for row in capsules:
                if rid and str(row.get("run_id") or "") == rid:
                    continue
                persisted_direction = row.get("direction")
                if not valid_live_direction(persisted_direction):
                    scope_unknown += 1
                    direction_unknown += 1
                    continue
                if persisted_direction != current_direction:
                    continue
                if tid and str(row.get("task_id") or "") == tid:
                    continue
                if not tid and not fp:
                    scope_unknown += 1
                    continue
                if not _capsule_fingerprint_scope_complete(row):
                    scope_unknown += 1
                    meta = _capsule_completeness(
                        row, "fingerprint", len(row.get("fingerprint") or []))
                    fingerprint_unknown += int(meta is None or meta[0] is None)
                    fingerprint_omitted += int(meta[1] or 0) if meta is not None else 0
            concept_scope = {
                "scope_complete": scope_unknown == 0,
                "scope_unknown_capsules": scope_unknown,
                "scope_fingerprint_unknown_capsules": fingerprint_unknown,
                "scope_fingerprint_items_omitted": fingerprint_omitted,
                "scope_direction_unknown_capsules": direction_unknown,
            }

            def _scoped(row, *, capsule: bool = False):
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
                if capsule:
                    from looplab.engine.memory import _capsule_fingerprint_scope_complete
                    # CODEX AGENT: capsule fingerprints are bounded durable projections. A capped or
                    # pre-receipt fingerprint may still support its exact task above, but cannot authorize
                    # fuzzy transfer into a different task's live Researcher prompt.
                    if not _capsule_fingerprint_scope_complete(row):
                        return False
                stored = [t for t in stored if not str(t).startswith("param:")]
                return fingerprint_similarity(fp, stored) >= 0.34

            lessons = _filter_claim_source_rows(lessons, _scoped, research=False)
            capsules = _filter_capsule_rows(capsules, lambda r: _scoped(r, capsule=True))
            research = _filter_claim_source_rows(
                load_research_claims(base),
                lambda r: ((not rid or str(r.get("run_id") or "") != rid)
                           and bool(tid) and str(r.get("task_id") or "") == tid
                           and same_live_direction(current_direction, r.get("direction"))),
                research=True,
            )
            # Freeze all three operator-policy ledgers together. The live prompt must never combine
            # an alias map from before a split with a claim overlay from after it.
            governance = _governance
            # Resolve the SAME taxonomy snapshot as the Atlas (aliases + splits), so a purged/merged/split
            # concept never leaks into the proactive prompt through this raw overview (CODEX).
            capsule_source = _capsule_source_summary(capsules)
            if capsules or capsule_source.get("source_complete") is not True:
                overview, concept_rows = _portfolio_concept_overview_data(
                    capsules, aliases=governance["aliases"],
                    splits=governance["splits"])
            else:
                overview, concept_rows = None, None
            # lessons + D8 claims + operator decisions; structured claim key when enabled (§21.20.13).
            claims = claims_for_memory(base, lessons=lessons, research_claims=research,
                                       decisions=governance["decisions"],
                                       structured=getattr(self, "_cross_run_structured_claims", False))
            claim_source = getattr(claims, "claim_source", {})
            if (not lessons and not overview and not research
                    and concept_scope["scope_complete"]
                    and isinstance(claim_source, dict)
                    and claim_source.get("source_complete") is True):
                self._cross_run_advisory_receipt = {}
                return ""
            # CODEX AGENT: live tendency selection consumes the exact scoped retained aggregate before the
            # overview's display cap; build_context_pack still bounds every model-visible list itself.
            pack = build_context_pack(
                claims, concept_overview=overview, _concept_rows=concept_rows)
            pack["concept_scope"] = concept_scope
            text = render_context_pack(pack)
            if not concept_scope["scope_complete"]:
                # CODEX AGENT: filtered unknown-scope capsules remain part of the model-visible receipt.
                # Otherwise a live prompt with zero eligible rows silently turns unknown applicability into
                # exact absence even though the fingerprint writer explicitly reported a lossy projection.
                text += ("\nCross-run capsule applicability scope is PARTIAL: "
                         f"{concept_scope['scope_unknown_capsules']} capsule(s) unclassified, "
                         f"{concept_scope['scope_fingerprint_items_omitted']} fingerprint item(s) known "
                         "omitted. Retained counts are lower bounds; absence is not proof.")
            text = cross_run_text(text, max_chars=16_000, single_line=False, entropy=True)
            # Digest the exact bounded structured pack behind the rendered prompt, not raw legacy stores.
            # A raw hash is both a credential oracle and an identity for bytes the model never received.
            corpus_projection = sanitize_cross_run_projection(
                pack, max_chars=64_000, max_items=64, max_total_items=2_048)
            corpus = json.dumps(corpus_projection,
                                ensure_ascii=False, sort_keys=True, default=str,
                                separators=(",", ":")).encode("utf-8")
            self._cross_run_advisory_receipt = {
                "v": 2,
                "scope_task": cross_run_text(
                    tid, max_chars=500, single_line=True, entropy=False),
                "excluded_run": cross_run_text(
                    rid, max_chars=500, single_line=True, entropy=False),
                "n_lessons": len(lessons), "n_capsules": len(capsules), "n_research": len(research),
                "concept_scope": concept_scope,
                "claim_source": claim_source,
                "corpus_digest": hashlib.sha256(corpus).hexdigest(),
                "render_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            return ("\n" + text) if text else ""
        except GovernanceLedgerUnavailable as exc:
            # CODEX AGENT: suppressing untrusted policy is safe; erasing its health state is not.
            # Keep a closed, content-free receipt so audit distinguishes disabled/empty from unavailable.
            self._cross_run_advisory_receipt = {
                "v": 2, "status": "unavailable", "complete": False,
                "governance": exc.public_receipt(),
            }
            return ""
        except Exception:  # noqa: BLE001 — advisory context is best-effort, never blocks proposing
            self._cross_run_advisory_receipt = {}
            return ""

    def _stamp_novelty_hint(self, state: RunState, stance: str, researcher=None) -> None:
        """Stamp the Strategist's novelty dial onto the ACTIVE researcher (slice 2/4): a prose
        directive `_novelty_hint` (+ the coverage gaps to act on) that the researcher folds into its
        prompt, plus the stance VALUE `_novelty_stance` the foresight ranker reads. "balanced" ->
        empty hint (byte-identical to today's prompt). Extracted so the DEBUG/repair path can force a
        NEUTRAL "balanced" stance — novelty pressure ("open a new direction") is wrong when the job is
        to FIX a failure — and so draft/improve refresh it from the live `self._novelty_stance` every
        node (no stale hint bleeds from a prior operator into a later one)."""
        nov_hint = ""
        if stance == "explore":
            # Reuse the newest STILL-LIVE snapshot (shared reverse-scan reader); a lifecycle edit at the
            # same node count invalidates every stale receipt and falls back to the generic explore cue.
            cov = latest_live_snapshot(state, state.coverage_snapshots)
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
                cs = latest_live_snapshot(state, state.concept_coverage_snapshots)
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
        # Variant-1: stamp THIS build's own researcher (a pool member), not the shared
        # `self.researcher` — otherwise a concurrent sibling build clobbers the novelty hint/stance
        # this researcher is about to read in `propose`, and the pooled build silently loses the
        # strategist's explore/capability-expansion directive (the plateau-jump escape).
        _r = researcher if researcher is not None else self.researcher
        for _attr, _val in (("_novelty_hint", nov_hint), ("_novelty_stance", stance)):
            try:
                setattr(_r, _attr, _val)
            except Exception:  # noqa: BLE001
                pass
