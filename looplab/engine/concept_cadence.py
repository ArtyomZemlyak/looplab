"""The PART IV/V concept cadence for the engine — split out of `engine/strategy.py` as its own
MIXIN (doc 25 EC-09): `class Engine(…, ConceptCadenceMixin)` inherits these methods unchanged, so
there is ZERO call-site churn and `self` here IS the engine, exactly as in every other engine mixin.

What lives here is one subsystem with one cadence of its own: the classifier RE-TAG /
consolidation / edge-assertion / hypothesis-tagging pass, the concept-coverage snapshot it feeds,
and the once-per-run concept-base seed. It shared a file with the Strategist consult only because
both are cadence work; they share no state, and the gate they run on is deliberately DIFFERENT —
`_should_consult_concepts` paces on `concept_retag_every` (default 30) because the LLM concept map
is heavier and slower-moving than a strategy consult, while the Strategist runs on
`strategist_every`. Keeping them in one file made that decoupling read as an accident.

`_concept_coverage_snapshot` is a DRIVER over named steps (`_refresh_concept_tags`, itself over
`_reusable_node_tags` and `_record_node_concept_tags`, then `_assert_concept_edges` and
`_tag_hypothesis_concepts`, then the coverage summary it returns — four DURABLE effects in the log:
consolidation renames, per-node tag rows, `is_a` edges, card tags).
The step boundaries are the ones the original 240-line body already had, marked
by its nested try blocks: the edge assertion and the hypothesis tagging each swallow their own
failures so a hiccup in audit enrichment cannot lose the snapshot, while a failure in the tagging
pass itself falls through to the outer guard and yields no snapshot at all. That asymmetry is the
whole reason the nesting exists, so each step OWNS its guard rather than the driver spelling four of
them in a row.

Layering: no runtime import of the orchestrator and never serve — only core, events, search and
stdlib (the concept-graph / lock-in producers stay lazy, method-local, as they were)."""
from __future__ import annotations

from typing import Optional

from looplab.core.concepts import MAX_MATERIALIZED_CONCEPTS, normalize_concept_id
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED, NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                  NODE_CONCEPT_PROVENANCE_OPERATOR, RunState,
                                  node_concept_event_provenance)
from looplab.engine.cadence import cadence_due, cadence_marks
from looplab.events.replay import fold
from looplab.events.types import (EV_CONCEPT_CONSOLIDATION, EV_CONCEPT_COVERAGE_SNAPSHOT,
                                  EV_CONCEPT_EDGE, EV_HYPOTHESIS_CONCEPTS, EV_NODE_CONCEPTS,
                                  EV_RUN_CONCEPTS)
from looplab.search.coverage import already_covered_at, analytics_projection_token

# HT (§21.18): max hypotheses to agentically tag per strategist cadence, so a large board (the rubertlite
# run hit ~150) tags incrementally over a few cadences instead of exploding one cadence's LLM budget.
_HYP_TAG_CAP = 60
# B1 (§21.18): a node whose tags were made against < _RETAG_GROWTH of the latest vocabulary is "stale" and
# gets re-tagged against the grown vocab; at most _RETAG_CAP such nodes per cadence (bounds the LLM cost,
# the rest refresh over subsequent cadences).
_RETAG_GROWTH = 0.7
_RETAG_CAP = 20


class ConceptCadenceMixin:
    """The engine's concept-cadence cluster. See the module docstring for the mixin convention
    (`self` is the Engine; `_op_span` stays on the Engine)."""

    def _should_consult_concepts(self, state: RunState, *, marks=None) -> bool:
        """PART V (F1): the concept CLASSIFIER re-tag / consolidation cadence — DECOUPLED from
        `strategist_every`. The LLM concept map is heavier and slower-moving than a strategy consult, so it
        refreshes on its OWN `concept_retag_every` interval (default 30) rather than every consult.
        Researcher-authored `idea.concepts` still fold into node_concepts at node_created (immediate UI
        freshness); this only paces the classifier-EVIDENCE + consolidation refresh and the concept-coverage
        pivot snapshot. Same shape/guards as `_should_consult` (creation decision point, seed boundary, then
        every interval — since-last, for the same batch-stride reason `_should_consult` states)."""
        if state.pending_nodes():
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        if n == self.n_seeds:
            return True
        every = getattr(self, "concept_retag_every", 0) or self.strategist_every
        return cadence_due(n, cadence_marks(marks), every)

    @in_llm_lane("enrichment")
    def _maybe_snapshot_concept_coverage(self, state: RunState) -> RunState:
        """PART IV Phase 2a: record a compact concept-graph coverage + uncovered-region snapshot at the
        `concept_retag_every` cadence (via `_should_consult_concepts`, NOT `strategist_every`) when
        `concept_pivot` is on. The producer is LLM-backed when a client is wired,
        with a deterministic heuristic fallback; recording the result makes replay deterministic. The
        folded record does not select a node directly, but its explore-stance directive can change future
        Researcher candidates. Same at_node idempotence gate as `_maybe_snapshot_coverage`; no-op
        off-cadence / mid-eval / when the flag is off / when neither a client nor fallback skeleton works."""
        # This gates on the seed boundary + ``concept_retag_every`` (via ``_should_consult_concepts``),
        # NOT ``strategist_every``. Every operator-facing description — this docstring, configuration.md,
        # the core config help, and the Settings-UI schema — is aligned to that cadence so operators tune
        # the control that actually governs snapshot freshness / paid-LLM cost.
        if (not getattr(self, "_concept_pivot", False)
                or not self._should_consult_concepts(
                    state, marks=state.concept_coverage_snapshots)):
            return state
        n = len(state.nodes)
        if already_covered_at(state, n, state.concept_coverage_snapshots):
            return state
        # KNOWN GAP (not a claim-fenced path, unlike the finalize stewards): this cadence dispatches
        # `_concept_coverage_snapshot`'s paid tagging/consolidation calls DIRECTLY, with no durable
        # invocation claim taken before the provider call. A crash between provider success and the
        # EV_NODE_CONCEPTS / EV_CONCEPT_COVERAGE_SNAPSHOT appends therefore re-purchases the
        # un-recorded tagging on resume. It is BOUNDED, not prevented: `_RETAG_CAP`/`_HYP_TAG_CAP`
        # limit each pass and per-node incremental reuse skips already-tagged nodes, so the repeat is
        # a small re-spend rather than the whole snapshot. Closing it means the claim-before-dispatch
        # protocol `curation_protocol.py::_paid_curation_attempt` implements — claim the bounded input
        # digest, persist terminal/ambiguous receipts — since an eventual snapshot is not a payment fence.
        snap = self._concept_coverage_snapshot(state)
        if snap is None:
            return state
        # The agentic producer may have persisted fresh tags/consolidation while building ``snap``. Bind the
        # snapshot to that post-write projection, not the stale state object passed into the cadence.
        current = fold(self.store.read_all())
        if already_covered_at(current, n, current.concept_coverage_snapshots):
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
            # the fold-derived missing-base receipt is the one exception: it proves that no
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
                # a bounded/invalid/otherwise partial membership cannot become an exact
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

    # ----------------------------------------------- the snapshot producer and its named steps
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
        from looplab.search.concept_analytics import concept_coverage, uncovered_regions
        from looplab.search.concept_graph import skeleton_for
        from looplab.search.lock_in import lock_in_signal
        # Defensive: a bare/None `self` (e.g. a unit test calling this as a pure helper) has no reflect
        # client -> deterministic fallback, unchanged behaviour. Real engines get the agentic path.
        _rc = getattr(self, "_reflect_client", None)
        client = _rc() if callable(_rc) else None
        seed = skeleton_for(state.task_id or "")
        seed = seed if seed.concepts() else None
        # guard the whole producer so a failed snapshot cannot perturb the run. A successfully
        # recorded snapshot is deliberately behavioral: its uncovered-region cue can steer later proposals.
        try:
            graph = tags = cov = None
            important: list = []
            mode = "heuristic"
            if client is not None:
                parser = getattr(getattr(self, "deep_researcher", None), "parser", "tool_call")
                cmap = self._refresh_concept_tags(state, client, parser, seed)
                # Reuse the coverage build_concept_map already computed (no second O(nodes) rollup).
                graph, tags = cmap["graph"], cmap["tags"]
                cov, important = cmap["coverage"], cmap["important_uncovered"]
                mode = cmap.get("mode", "llm")
                self._assert_concept_edges(state, graph, mode)
                self._tag_hypothesis_concepts(state, graph, client, parser, mode)
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

    def _reusable_node_tags(self, state: RunState) -> tuple[dict, dict]:
        """The per-node tags this cadence may REUSE (so `build_concept_map` only pays for the rest) and
        the consolidation renames it must keep FIXED. Pure over `state`; no appends, no LLM."""
        # INCREMENTAL (§21.16, Phase 2c): reuse per-node tags already recorded as `node_concepts`
        # events and only LLM-tag NEW nodes, so per-run tagging is ~O(nodes) not ~O(nodes × cadences).
        # Bare-library EngineOptions keeps `_concept_pivot` off, while product `Settings` is ON;
        # cadence, bounded per-pass work, and incremental reuse therefore bound product cost.
        # The snapshot producer uses `tools=None` and tags from the node's
        # RECORDED text (idea/params/result/log excerpts in state), i.e. mode="llm", NOT a live
        # per-node tool-loop (passing run tools would make it fully agentic but far heavier).
        from looplab.search.concept_tagging import stale_tagged_nodes
        provenance = getattr(state, "node_concept_provenance", None) or {}
        # Researcher-authored Idea.concepts are visible read-model claims, not an
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
        return known, known_renames

    def _refresh_concept_tags(self, state: RunState, client, parser, seed) -> dict:
        """Build the concept map over the run's recorded text and durably record what is NEW in it —
        the consolidation renames and the per-node tag rows. Returns the raw `build_concept_map`
        result, whose graph/tags/coverage the driver reuses. NO guard of its own on purpose: a failed
        tagging pass has nothing to summarize, so it falls through to the driver's outer guard and
        yields no snapshot (unlike the two enrichment steps below, which swallow their own failures)."""
        import contextlib

        from looplab.search.concept_map import build_concept_map
        known, known_renames = self._reusable_node_tags(state)
        # Span-scope the concept-map LLM generations (tagging + consolidation + importance) so they
        # file under a `concept_coverage` op, not the ambient/next-node trace. nullcontext if spanless.
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
        self._record_node_concept_tags(state, cmap, known)
        return cmap

    def _record_node_concept_tags(self, state: RunState, cmap: dict, known: dict) -> None:
        """Append one `node_concepts` row per node whose tags are NEW, re-tagged, or merely refreshed
        against a grown vocabulary — the durable trust boundary the classifier-evidence channel reads."""
        mode = cmap.get("mode", "llm")
        graph = cmap["graph"]
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
            # one classifier batch may contain per-node heuristic fallbacks. Stamp
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
                # a classifier row outside its reviewed schema is only a bounded
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

    def _assert_concept_edges(self, state: RunState, graph, mode: str) -> None:
        """Record the NEW `is_a` parent assertions the graph carries (path/curated structure), once."""
        # do not persist co-tag edges. They can decrease or disappear after
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

    def _tag_hypothesis_concepts(self, state: RunState, graph, client, parser, mode: str) -> None:
        """Tag the untagged / most-stale cards against the SAME graph, bounded per cadence."""
        # HT (§21.18): agentically tag any UNtagged hypotheses against the SAME graph and record
        # them, so taxonomy dedup reuses the agentic tags instead of the tag_text alias heuristic.
        # Incremental (skip already-tagged) + capped per cadence, so a big board tags over a few
        # cadences instead of exploding one. Isolated try: a tagging hiccup must not lose the snapshot.
        try:
            from looplab.search.concept_tagging import stale_tagged_nodes, tag_text_llm
            known_h = getattr(state, "hypothesis_concepts", None) or {}
            h_at_vocab = getattr(state, "hypothesis_concepts_at_vocab", None) or {}
            v_now = len(graph.concepts())
            # B1-ext (§21.18): re-tag the most-STALE hypotheses (tagged against a much smaller vocab)
            # in addition to UNtagged ones — same at_vocab staleness rule as nodes, bounded per cadence.
            stale_h = set(stale_tagged_nodes(list(known_h), h_at_vocab,
                                             growth=_RETAG_GROWTH, cap=_RETAG_CAP))
            tagged_this_cadence = 0
            # 1 card = 1 hypothesis: tag the single Card board. Tag the DISPLAY `statement` (== the
            # old Hypothesis.statement, including a merged card's consolidated wording). The cache
            # is joined by the card id AND its seed-statement hash (below): a research card without
            # an explicit card_id already has that hash as its id, but a migrated native card-N does
            # not, so an id-only join would orphan its legacy tags.
            from looplab.core.models import hypothesis_concept_cache_keys
            for h in state.research_cards():
                if not getattr(h, "statement", ""):
                    continue
                # Match the recorded tags by the card id OR its seed-statement hash (peer review),
                # and judge staleness on the MATCHED key — an id-only skip re-tagged a migrated
                # card-N belief every resume and mis-read its freshness.
                matched = next((k for k in hypothesis_concept_cache_keys(h) if k in known_h), None)
                if matched is not None and matched not in stale_h:   # already tagged & fresh -> skip
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
