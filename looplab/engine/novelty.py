"""Novelty / dedup gate (E1/T5) for the engine — extracted from orchestrator.py as a MIXIN:
`class Engine(NoveltyGateMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (`_embedder` / `_idea_vecs` cache / `store` / `researcher` /
`_novelty_*` knobs / `_reflect_client`), exactly as they did inside the class.

Two layers, cheapest first, BEFORE any compute is spent on a proposal: a SEMANTIC/LLM
near-duplicate check (reject + one informed re-propose) and the E1 NUMERIC param-distance nudge.
The heavy tool/agent imports stay method-local (imported from their source modules on use), so a
test monkeypatching `looplab.tools.vectorstore._cosine` / `looplab.agents.agent.agentic_struct`
still intercepts them.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only
core, events and stdlib (the search/agent/tool deps are lazy, method-local imports)."""
from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                  NODE_CONCEPT_PROVENANCE_OPERATOR, Idea, NodeStatus, RunState)
from looplab.events.types import EV_CROSS_RUN_PRIOR, EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED


_IDEA_IDENTITY_MAX_SOURCE_CHARS = 16_384
_IDEA_IDENTITY_MAX_NORMALIZED_CHARS = 32_768
_IDEA_IDENTITY_MAX_TOKENS = 2_048
_IDEA_IDENTITY_CACHE_MAX = 1_024
_LOG = logging.getLogger(__name__)


def _canonical_idea_identity(text: str) -> tuple[tuple[str, ...], bool]:
    """Return a bounded, punctuation-insensitive identity and whether it is complete.

    Compatibility normalization and case-folding close cheap surface-form bypasses (for example full-
    width text or ``strasse``/``Straße``), while Unicode punctuation, symbols, controls and whitespace
    are separators. The completeness bit lets the admission gate fail closed instead of treating a common
    truncated prefix as proof that two arbitrarily large proposals differ.
    """
    raw = str(text or "")
    complete = len(raw) <= _IDEA_IDENTITY_MAX_SOURCE_CHARS
    normalized = unicodedata.normalize(
        "NFKC", raw[:_IDEA_IDENTITY_MAX_SOURCE_CHARS]
    ).casefold()
    if len(normalized) > _IDEA_IDENTITY_MAX_NORMALIZED_CHARS:
        normalized = normalized[:_IDEA_IDENTITY_MAX_NORMALIZED_CHARS]
        complete = False

    tokens: list[str] = []
    token: list[str] = []
    # Boundary care at exactly _IDEA_IDENTITY_MAX_TOKENS: a text whose 2048th token is flushed by a
    # trailing SEPARATOR must NOT be marked incomplete just for the separator — the byte-identical text
    # without it flushes token #2048 in the for/else and stays complete. Completeness carries real weight
    # (an "incomplete" prior permanently self-disables the level-4/5 graded-novelty short-circuit), so only
    # flip complete=False when token-worthy content GENUINELY remains past the cap.
    for i, char in enumerate(normalized):
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or (token and category[0] == "M"):
            token.append(char)
            continue
        if token:
            tokens.append("".join(token))
            token = []
            if len(tokens) >= _IDEA_IDENTITY_MAX_TOKENS:
                # A new token can only START on an L/N char, so genuine truncation requires one ahead.
                if any(unicodedata.category(c)[0] in {"L", "N"} for c in normalized[i + 1:]):
                    complete = False
                break
    else:
        if token:
            tokens.append("".join(token))
    return tuple(tokens), complete


def _same_canonical_idea_identity(
    left: tuple[tuple[str, ...], bool],
    right: tuple[tuple[str, ...], bool],
) -> bool:
    """Return whether two complete, non-empty identities are surface variants."""
    left_tokens, left_complete = left
    right_tokens, right_complete = right
    if not left_complete or not right_complete or not left_tokens or not right_tokens:
        return False
    return left_tokens == right_tokens or "".join(left_tokens) == "".join(right_tokens)


class NoveltyGateMixin:
    """The engine's novelty/dedup gate cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    # -------------------------------------------------------- novelty gate (E1/T5)
    @staticmethod
    def _idea_text(idea) -> str:
        """The semantic identity of a proposal: what it claims to try + why."""
        return " ".join(filter(None, [getattr(idea, "rationale", "") or "",
                                      getattr(idea, "hypothesis", "") or ""])).strip()

    def _idea_vec(self, text: str):
        # Key on the TEXT (not a node_id): the embedding is a pure function of the text, and a
        # `node_reset` re-creates the SAME id with a NEW idea — a node_id-keyed cache then returned the
        # OLD vector and the semantic-novelty gate compared future proposals against a stale idea. The
        # cache is in-memory only (never persisted/replayed), so a per-process `hash(text)` key is safe.
        key = hash(text)
        v = self._idea_vecs.get(key)
        if v is None:
            v = self._embedder(text)
            self._idea_vecs[key] = v
        return v

    def _cached_prior_idea_identity(self, text: str) -> tuple[tuple[str, ...], bool]:
        """Memoize immutable prior-node identities with a bounded, collision-free content key."""
        raw = str(text or "")
        # The canonicalizer never reads beyond its source cap. Length + cap+1 exact characters therefore
        # distinguish every complete input and every materially different truncated result without retaining
        # an unbounded model-supplied rationale in the cache key.
        key = (len(raw), raw[:_IDEA_IDENTITY_MAX_SOURCE_CHARS + 1])
        cache = getattr(self, "_idea_identity_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._idea_identity_cache = cache
        if key not in cache:
            if len(cache) >= _IDEA_IDENTITY_CACHE_MAX:
                cache.clear()
            cache[key] = _canonical_idea_identity(raw)
        return cache[key]

    def _warn_incomplete_prior_identity(self, state: RunState, node) -> None:
        """Expose a fail-closed oversized-prior cliff once per run/node lifecycle."""
        seen = getattr(self, "_idea_identity_warnings", None)
        if not isinstance(seen, set):
            seen = set()
            self._idea_identity_warnings = seen
        key = (state.run_id, node.id, getattr(node, "attempt", 0))
        if key in seen:
            return
        if len(seen) >= _IDEA_IDENTITY_CACHE_MAX:
            seen.clear()
        seen.add(key)
        _LOG.warning(
            "graded novelty bypass deferred: prior node %s has an incomplete bounded idea identity",
            node.id,
        )

    def _semantic_duplicate(self, state: RunState, idea: Idea):
        """T5: nearest existing node by idea-TEXT embedding similarity, or None. Only meaningful
        for proposals with real text (LLM ideas); short/empty rationales (toy backends) skip."""
        text = self._idea_text(idea)
        if len(text) < 20:
            return None, 0.0
        from looplab.tools.vectorstore import _cosine
        v = self._embedder(text)
        best_n, best_s = None, 0.0
        for n in state.nodes.values():
            nt = self._idea_text(n.idea)
            if len(nt) < 20:
                continue
            try:
                s = _cosine(v, self._idea_vec(nt))
            except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                continue
            if s > best_s:
                best_n, best_s = n, s
        if best_n is not None and best_s >= self._novelty_semantic_threshold:
            return best_n, best_s
        return None, best_s

    def _llm_novelty_gate(self, state: RunState, idea: Idea, repropose=None, researcher=None) -> Idea:
        """novelty_mode="llm": an LLM (not an embedding/param-distance heuristic) judges whether the
        proposed idea near-duplicates an already-tried experiment — READING the real experiments via
        tools when unsure — and, if it does and a `repropose` callable is given, asks the Researcher once
        more for a meaningfully different idea (surfacing the duplicate's outcome). Loop-safe + best-
        effort: any failure just returns the original idea. Emits the same `novelty_rejected` audit
        event (kind="llm") the algorithmic gate does."""
        if not state.nodes:
            return idea
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return idea
        from pydantic import BaseModel
        from looplab.agents.agent import agentic_struct, CompositeTools
        from looplab.tools.run_tools import RunTools

        class _NoveltyVerdict(BaseModel):
            is_duplicate: bool = False
            near_node_id: Optional[int] = None
            reason: str = ""

        brief = "; ".join(f"#{n.id} {n.operator}: {self._idea_text(n.idea)[:80]}"
                          for n in list(state.nodes.values())[-25:])
        msgs = [{"role": "system",
                 "content": "You judge experiment NOVELTY for an ML research loop. Decide if a PROPOSED "
                            "idea is a near-duplicate of an experiment already tried in THIS run. Read the "
                            "actual experiments (read_experiment / read_code) when unsure. A rewording or a "
                            "trivially-close variant of a tried idea is a DUPLICATE; a genuinely different "
                            "approach, component, loss, data or direction is NOVEL. Prefer NOVEL unless "
                            "clearly a repeat."},
                {"role": "user",
                 "content": f"PROPOSED idea: {self._idea_text(idea)}\n\nAlready tried: {brief}\n\n"
                            "Emit is_duplicate, near_node_id (the tried experiment it duplicates, or null), "
                            "and a one-line reason."}]
        try:
            rt = RunTools()
            rt.bind_state(state, None)
            v = agentic_struct(client, CompositeTools([rt]), msgs, _NoveltyVerdict,
                               loop_opts={"max_turns": 12})
        except Exception:  # noqa: BLE001
            return idea
        if not (v and getattr(v, "is_duplicate", False)
                and isinstance(v.near_node_id, int) and v.near_node_id in state.nodes):
            return idea
        dup = state.nodes[v.near_node_id]
        outcome = (f"it FAILED ({dup.error_reason})" if dup.status is NodeStatus.failed
                   else f"it scored {dup.metric}")
        self.store.append(EV_NOVELTY_REJECTED, {
            # the PROSPECTIVE id this proposal would get — allocated as max+1, NOT len(): on a gapped
            # log (a dropped/malformed node_created) len() points at the wrong slot (audit only).
            "node_id": max(state.nodes, default=-1) + 1, "near_node": dup.id, "kind": "llm",
            "reason": str(v.reason)[:200], "stance": self._novelty_stance,
            "action": "reproposed" if callable(repropose) else "kept"})
        if callable(repropose):
            hint = (f"\nNOVELTY GATE (LLM): your proposal near-duplicates experiment #{dup.id} — "
                    f"{outcome} ({str(v.reason)[:160]}). Propose something MEANINGFULLY DIFFERENT "
                    "(another approach, component or direction), not a rewording.")
            idea = self._repropose_with_feedback(repropose, hint, idea, researcher=researcher)
        return idea

    def _repropose_with_feedback(self, repropose, hint: str, idea: Idea, researcher=None) -> Idea:
        """One informed re-propose with the duplicate surfaced as a TRANSIENT `_novelty_feedback`
        directive (shared by the LLM and semantic gates — the set/try/finally-restore discipline
        must stay identical in both). `BudgetExceeded` re-raises (the hard budget stop must end the
        run, not be swallowed); any other repropose failure keeps the original idea. The `finally`
        ALWAYS restores the previous feedback, even if repropose() raised: otherwise this transient
        "you are duplicating #N" directive leaks into EVERY later proposal in the run — including
        drafts in unrelated regions — permanently mis-steering the researcher away from a direction
        the operator never banned."""
        _r = researcher if researcher is not None else self.researcher   # Variant-1: this build's researcher
        prev = getattr(_r, "_novelty_feedback", "")
        setattr(_r, "_novelty_feedback", hint)
        try:
            idea2 = repropose()
            if idea2 is not None:
                idea = idea2
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — a repropose failure keeps the original idea
            pass
        finally:
            setattr(_r, "_novelty_feedback", prev)
        return idea

    def _intra_batch_dup(self, idea, chosen: list) -> bool:
        """Variant-1 Phase 2: is `idea` a near-duplicate of one already chosen in THIS proposal batch,
        by idea TEXT? Mirrors the semantic gate's 20-char floor — param-only ideas (toy backends whose
        rationale is a constant like 'random seed point') have no meaningful text identity and are
        NEVER text-deduped here (their diversity lives in the numeric params, which the normal novelty
        gate already handles), so batch diversity for LLM ideas never collapses distinct toy seeds."""
        raw = self._idea_text(idea)
        t = raw.strip().lower()
        if len(t) < 20:
            return False
        # Semantic parity with the serial draft path (review finding #7): when a run actually uses the
        # novelty gate, two batch siblings that are embedding-near but textually DISTINCT are still
        # duplicates (the serial path would catch the second against the first, already in history).
        # Guarded on the novelty mode so toy/off runs stay text-only + byte-identical; best-effort, so an
        # embedder hiccup silently degrades to text dedup. The new idea's vector is embedded at most once.
        _semantic = getattr(self, "_novelty_mode", "off") not in (None, "off")
        _vec = None
        for other in chosen:
            ot_raw = self._idea_text(other)
            ot = ot_raw.strip().lower()
            if len(ot) < 20:
                continue
            if t == ot:
                return True
            # A pure-substring test would wrongly merge a short idea into a much LONGER distinct one
            # ("use a deeper net" vs "use a deeper net WITH cross-attention"). Only treat containment as
            # a near-duplicate when the two texts are also close in LENGTH (the longer isn't materially
            # extending the shorter with new content).
            if (t in ot or ot in t):
                lo, hi = sorted((len(t), len(ot)))
                if hi <= lo * 1.25:
                    return True
            if _semantic:
                try:
                    from looplab.tools.vectorstore import _cosine
                    if _vec is None:
                        _vec = self._embedder(raw)
                    if _cosine(_vec, self._idea_vec(ot_raw)) >= self._novelty_semantic_threshold:
                        return True
                except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                    pass
        return False

    def _propose_batch(self, state: RunState, n: int) -> list:
        """Variant-1 Phase 2 — the ONE shared-researcher pass that yields up to N DISTINCT seed
        hypotheses for a concurrent draft batch ("one researcher -> N ideas -> N developers"), so a
        `parallel_build>1` fan-out doesn't pay N independent research rolls that collide on the same
        direction. A backend MAY expose a true one-call `propose_batch(state, n)`; otherwise we roll
        the researcher's own `propose` up to a bounded number of times, each time surfacing the
        directions already taken THIS batch as a transient avoidance directive (reusing the
        `_novelty_feedback` channel the researcher already reads), applying the normal vs-history
        novelty gate, and DROPPING an intra-batch near-duplicate. Distinct-by-construction and
        backend-agnostic; returns 1..N ideas (fewer only if the researcher can't diversify). Runs in
        the MAIN task before the build fan-out, so it uses `self.researcher` (no pool race)."""
        n = max(1, int(n))
        native = getattr(self.researcher, "propose_batch", None)
        ideas: list = []
        if callable(native):
            try:
                produced = list(native(state, n)) or []
                for idea in produced:
                    if idea is not None and not self._intra_batch_dup(idea, ideas):
                        ideas.append(idea)
                if ideas:
                    # A native batch backend proposes all N at once, so per-idea FOREAGENT telemetry
                    # isn't separable here — leave it to the backend to emit its own (no per-node stamp).
                    self._pending_batch_telemetry = [None] * len(ideas[:n])
                    return ideas[:n]
            except Exception:  # noqa: BLE001 — a batch-backend hiccup falls back to sequential rolls
                ideas = []
        prev_feedback = getattr(self.researcher, "_novelty_feedback", "")
        # Capture EACH accepted roll's FOREAGENT telemetry (hypothesis ranking + foresight pick the
        # researcher set during propose) BEFORE the next roll overwrites it on the shared researcher, so
        # each pooled build can later emit hypothesis_ranked/foresight_selected for ITS OWN idea instead
        # of the last roll's (mega-review MEDIUM). Aligned 1:1 with the returned ideas.
        telem: list = []
        attempts, max_attempts = 0, n * 2 + 2
        try:
            while len(ideas) < n and attempts < max_attempts:
                attempts += 1
                if ideas:
                    taken = "; ".join(filter(None, (self._idea_text(x)[:200] for x in ideas)))
                    if taken:
                        setattr(self.researcher, "_novelty_feedback",
                                ("BATCH DIVERSITY: other drafts building CONCURRENTLY in this batch already "
                                 "take these directions — propose a MEANINGFULLY DIFFERENT axis / theme / "
                                 f"component, not a variation of: {taken}"))
                self._set_complexity_hint(state, None)          # A0d cues on the shared researcher
                idea = self.researcher.propose(state, None)
                if idea is None:
                    continue
                # Normal vs-history novelty gate (one informed re-propose on a semantic hit), exactly as
                # the serial draft path runs it — then drop an intra-batch near-duplicate.
                idea = self._apply_novelty_gate(
                    state, idea, repropose=lambda: self.researcher.propose(state, None))
                if idea is None or self._intra_batch_dup(idea, ideas):
                    continue
                ideas.append(idea)
                telem.append({
                    "last_hyp_priority": self._snapshot_role_telemetry("last_hyp_priority"),
                    "last_foresight": self._snapshot_role_telemetry("last_foresight"),
                    # THIS roll's cross-run advisory receipt (set on self by _set_complexity_hint above)
                    # so each pooled build stamps ITS OWN node_created provenance, not the last roll's.
                    "_cross_run_advisory_receipt": dict(getattr(self, "_cross_run_advisory_receipt", {}) or {}),
                })
        finally:
            setattr(self.researcher, "_novelty_feedback", prev_feedback)
            # Clear the shared researcher's per-roll telemetry: build 0 reuses self.researcher as its
            # pooled role, so a leftover last-roll ranking would otherwise be emitted against build 0's
            # node. Each build restamps its OWN captured snapshot in _create_node.
            for _a in ("last_hyp_priority", "last_foresight"):
                try:
                    setattr(self.researcher, _a, None)
                except Exception:  # noqa: BLE001
                    pass
        self._pending_batch_telemetry = telem
        return ideas

    def _snapshot_role_telemetry(self, attr: str):
        """A shallow copy of a researcher predictive-telemetry attr (a dict set during propose), or None.
        Copied so a later consume (setattr None) on the shared researcher can't blank the captured value."""
        val = getattr(self.researcher, attr, None)
        return dict(val) if isinstance(val, dict) else None

    def _graded_novelty_precheck(self, state: RunState, idea: Idea):
        """PART IV D3 (§21.4, Phase 2b): a concept-graph-aware PRE-gate. When `graded_novelty` is on and
        the task has a curated concept skeleton, grade the proposal over the concept graph BEFORE the flat
        dedup gate runs. Returns the idea UNCHANGED (an allow decision that SHORT-CIRCUITS the flat gate)
        for the two grades the flat LLM/semantic gate gets WRONG:
          * level 4 `same_direction_new_impl` — shares a concept BRANCH with a tried node but is a
            materially different implementation. The flat gate can't tell "this DCL tweak" from "the whole
            DCL branch" and would wrongly reject/repropose a legitimate variant.
          * level 5 `wrongly_abandoned` — re-opens a FAILED direction (every experiment touching it failed).
            The flat gate has NO re-open path; it would treat a sound-but-killed direction as a dead end.
        Returns None (defer to the flat gate, UNCHANGED behavior) for every other grade: level 0 (novel,
        the flat gate passes it anyway) and levels 1/2/3 (identical / near-dup / prior-run, which the flat
        gate legitimately dedups). Audit-only: the allow is recorded as a `novelty_graded` event, never a
        selection change. No-op (returns None) with the flag off, an empty run, or no vocabulary — so the
        default path is byte-identical.

        AGENTIC-FIRST (§21.4 F2): when independently classified node tags are cached as `node_concepts`,
        the grade uses only entries carrying an exact classifier provenance receipt (reconstructed
        deterministically — no re-tag). Researcher-authored memberships and unknown provenance never enter
        this bypass. It degrades to the skeleton + deterministic heuristic when the trusted cache is empty."""
        if not getattr(self, "_graded_novelty", False) or not state.nodes:
            return None
        from looplab.search.concept_graph import _experiment_nodes, graph_from_node_concepts, skeleton_for
        from looplab.search.graded_novelty import grade_novelty, tag_idea_llm
        seed = skeleton_for(state.task_id or "")
        seed = seed if seed.concepts() else None
        all_node_concepts = getattr(state, "node_concepts", None) or {}
        concept_provenance = getattr(state, "node_concept_provenance", None) or {}
        receipts = getattr(state, "node_concept_materialization_receipts", None) or {}
        # CODEX AGENT: an Idea's concepts are authored by the same proposer whose admission is being
        # decided, so they cannot certify their own graded-novelty bypass. Only replay-proven
        # `node_concepts` classifier events enter the agentic path; missing/unknown provenance fails
        # closed to the curated heuristic path (or no-op when no curated vocabulary exists).
        experiment_ids = {nd.id for nd in _experiment_nodes(state)}
        if any(
            nid in receipts
            and concept_provenance.get(nid) in {
                NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR}
            for nid in experiment_ids
        ):
            # A retained subset cannot certify complete experiment coverage or an L4/L5 bypass. This
            # also prevents an emptied partial cache from silently falling back to the heuristic path.
            return None
        classifier_ids = {
            nid for nid in experiment_ids
            if nid in all_node_concepts
            and concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_CLASSIFIER
            and nid not in receipts
        }
        # An operator-edited node has an authoritative human/assistant-asserted tag set, so it counts as
        # COVERED for the completeness check below — but its tags never enter `node_concepts` (the graded
        # channel stays classifier-only). Without this, a single operator edit leaves that node forever
        # outside `classifier_ids`, so the coverage gate below trips on every subsequent cadence and
        # disables the agentic graded-novelty path for the WHOLE run.
        operator_ids = {
            nid for nid in experiment_ids
            if concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_OPERATOR
            and nid not in receipts
        }
        # CODEX AGENT: a partial cadence is UNKNOWN coverage, not evidence that the remaining nodes
        # touch no concepts. Once any classifier receipt exists, every experiment must be covered (a
        # classifier receipt — an explicit empty list is valid — or an operator assertion) before this
        # precheck may issue an L4/L5 admission override. Otherwise a post-cadence near-repeat or win
        # could disappear from the grade; defer to the ordinary flat gate without mixing
        # proposer-authored labels into the trusted channel.
        if classifier_ids and (classifier_ids | operator_ids) != experiment_ids:
            return None
        node_concepts = {
            nid: all_node_concepts[nid]
            for nid in classifier_ids
        }
        # Everything below is guarded: a reconstruction / tagger / grader hiccup must NEVER block proposing.
        try:
            if node_concepts:                     # AGENTIC: reuse the cached LLM node tags (no re-tag)
                graph, tags = graph_from_node_concepts(node_concepts, seed_graph=seed)
            elif seed is not None:                # FALLBACK: curated skeleton + heuristic node tags
                graph, tags = seed, None
            else:
                return None                       # no vocabulary at all -> nothing to grade
            if not graph.concepts():
                return None
            # Tag the PROPOSED idea CONSISTENTLY with the node tags: only go agentic when the node tags are
            # agentic (the cache is present) so idea+node tags share one production rule; without the cache,
            # node tags are heuristic, so tag the idea heuristically too (idea_tags=None -> tag_idea inside
            # grade_novelty). This keeps the no-cache path fully deterministic (byte-identical to pre-F2) and
            # avoids a per-proposal LLM call that would have nothing agentic to be consistent with.
            _rc = getattr(self, "_reflect_client", None)
            client = _rc() if callable(_rc) else None
            idea_tags = (tag_idea_llm(idea, graph, client)
                         if (node_concepts and client is not None) else None)
            # §21.20 Step 2: the gating grade is computed WITHOUT cross-run priors, so enabling the flag is
            # byte-identical to cross-run-off for SELECTION (grade_novelty checks its level 3 before the
            # same-run level 4, so feeding priors here would flip an L4 allow into a defer — not audit-only).
            grade = grade_novelty(state, idea, graph, tags=tags, idea_tags=idea_tags)
        except Exception:  # noqa: BLE001 — a grader/tagger/reconstruction hiccup must never block proposing
            return None
        # §21.20 Step 2: cross-run priors are AUDIT-ONLY and computed SEPARATELY from the grade above — we
        # SURFACE an earlier run's outcome (a `cross_run_prior` event) only when the idea's OWN concepts
        # overlap a prior run's, and it NEVER changes the selection decision. Best-effort throughout.
        prior_set, prior_caps, aliases, splits = self._cross_run_prior(state)
        if prior_set:
            try:
                from looplab.engine.concept_registry import canonicalize_concepts
                from looplab.search.graded_novelty import tag_idea
                raw_concepts = list(idea_tags) if idea_tags is not None else list(tag_idea(idea, graph))
                idea_concepts = set(canonicalize_concepts(raw_concepts, aliases=aliases, splits=splits))
            except Exception:  # noqa: BLE001 — a tagger hiccup just means no surfacing
                idea_concepts = set()
            matched = idea_concepts & prior_set
            if matched:
                self._record_cross_run_prior(state, matched, prior_caps)
        # Only levels 4/5 are ALLOW-overrides of the flat gate. Level 0 (novel) and 1/2/3 (dedup) defer.
        if grade.level not in (4, 5):
            return None
        # grade_novelty's own dedup (levels 1/2) is PARAM-based, so empty/key-disjoint params can skip it
        # and make a textual repeat reach a level-4/5 ALLOW. Compare against EVERY tried node, not merely
        # `grade.near_node`: the concept-sharing node selected by the grader need not be the duplicated one.
        # CODEX AGENT: a graded score is never sufficient evidence of a new implementation. A level-4/5
        # short-circuit is allowed only when its bounded canonical prose is complete, non-empty, and differs
        # from every prior proposal after NFKC, Unicode case-folding and punctuation/whitespace separation.
        # Oversize/empty identities defer to the flat gate because we cannot prove a concrete difference.
        candidate_identity = _canonical_idea_identity(self._idea_text(idea))
        identity, complete = candidate_identity
        if not complete or not identity:
            return None
        for nd in state.nodes.values():
            if getattr(nd, "idea", None) is None:
                continue
            prior_canonical = self._cached_prior_idea_identity(self._idea_text(nd.idea))
            prior_identity, prior_complete = prior_canonical
            if not prior_complete:
                self._warn_incomplete_prior_identity(state, nd)
                return None                     # ambiguous prior -> visible, fail-closed flat-gate defer
            if _same_canonical_idea_identity(candidate_identity, prior_canonical):
                return None                      # duplicate/ambiguous identity -> defer to the flat gate
        near = state.nodes.get(grade.near_node)
        if near is None or getattr(near, "idea", None) is None:
            return None
        candidate_change = (dict(idea.params or {}), dict(idea.space or {}))
        prior_change = (dict(near.idea.params or {}), dict(near.idea.space or {}))
        # Different prose is a model assertion, not evidence of a different implementation. Until an
        # implementation/patch identity exists, only an explicit param or search-space delta can justify
        # skipping the ordinary semantic/LLM duplicate gate; prose-only variants must still pass it.
        if not any(candidate_change) or candidate_change == prior_change:
            return None
        self.store.append(EV_NOVELTY_GRADED, {
            # prospective id = max+1, not len() (gap-safe; audit only) — matches the reject events below.
            "node_id": max(state.nodes, default=-1) + 1, "level": grade.level, "grade": grade.name,
            "recommendation": grade.recommendation, "near_node": grade.near_node,
            "shared_concepts": list(grade.shared_concepts), "stance": self._novelty_stance,
            "rationale": str(grade.rationale)[:200]})
        return idea

    _CROSS_RUN_MIN_SIM = 0.3   # task-fingerprint Jaccard floor for a prior run to count as "similar"

    def _cross_run_prior(self, state: RunState):
        """Return canonical prior concepts/capsules plus the taxonomy snapshot used for proposal tags.
        for tasks SIMILAR to this run's fingerprint. (set(), []) when `cross_run_concepts` is off / no
        memory dir / store empty. Best-effort — any hiccup yields no priors so proposing is never blocked."""
        if not getattr(self, "_cross_run_concepts", False) or not getattr(self, "memory_dir", ""):
            return set(), [], {}, {}
        try:
            from pathlib import Path
            from looplab.engine.concept_registry import (canonicalize_concept, canonicalize_concepts,
                                                         load_concept_aliases, load_concept_splits)
            from looplab.engine.memory import ConceptCapsuleStore, _capsule_completeness
            # NOTE (full-CR TODO, §21.20.13 CR2a): this reloads+scans the whole capsule JSONL per proposal;
            # a bounded, versioned, scope-keyed query index replaces it once the retrieval planner lands.
            store = ConceptCapsuleStore(Path(self.memory_dir) / "concept_capsules.jsonl")
            fp = self.lessons.task_fingerprint(state, state.best())
            # HARD direction gate (CODEX): a min/rmse task and a max/recall task can share enough goal
            # tokens to clear the fuzzy Jaccard floor, but their outcomes are not comparable — require the
            # same optimization direction before a prior counts (a lean stand-in for the full contract).
            my_dir = str(getattr(state, "direction", "") or "min")
            caps = [(s, c) for s, c in store.prior_capsules(
                        fp, min_sim=self._CROSS_RUN_MIN_SIM,
                        exclude_run_id=getattr(state, "run_id", "") or "",
                        task_id=getattr(state, "task_id", "") or "")
                    if str(c.get("direction") or "min") == my_dir]
            aliases = load_concept_aliases(self.memory_dir)
            splits = load_concept_splits(self.memory_dir)
            prior: set[str] = set()
            canonical_caps = []
            for similarity, capsule in caps:
                raw = [str(x) for x in (capsule.get("concepts") or []) if str(x)]
                concept_meta = _capsule_completeness(capsule, "concepts", len(raw))
                raw_outcomes = capsule.get("concept_outcomes") or {}
                outcome_meta = _capsule_completeness(
                    capsule, "concept_outcomes",
                    len(raw_outcomes) if isinstance(raw_outcomes, dict) else 0,
                )
                if concept_meta is None or outcome_meta is None:
                    continue
                concepts = canonicalize_concepts(raw, aliases=aliases, splits=splits)
                outcomes = {}
                if isinstance(raw_outcomes, dict):
                    for source in sorted(raw_outcomes, key=str):
                        target = canonicalize_concept(source, sibling_concepts=raw,
                                                      aliases=aliases, splits=splits)
                        if not target or target not in concepts:
                            continue
                        candidate, current = raw_outcomes[source], outcomes.get(target)
                        # CODEX AGENT: several raw aliases can collapse to one canonical concept. Mirror
                        # portfolio_concept_overview: retain the best observation in this run's direction,
                        # never the value of whichever raw spelling sorts first.
                        better = (current is None and candidate is not None) or (
                            current is not None and candidate is not None
                            and ((candidate < current) if my_dir == "min" else (candidate > current))
                        )
                        if target not in outcomes or better:
                            outcomes[target] = candidate
                # CODEX AGENT: canonicalization can merge/purge retained labels, so completeness cannot be
                # recomputed from the transformed list. Freeze the already-validated raw capsule receipt
                # before replacing membership with its governed projection.
                source_receipt = {
                    "concepts_total": concept_meta[0],
                    "concepts_omitted": concept_meta[1],
                    "concepts_complete": concept_meta[2],
                    "concept_outcomes_total": outcome_meta[0],
                    "concept_outcomes_omitted": outcome_meta[1],
                    "concept_outcomes_complete": outcome_meta[2],
                }
                normalized = {
                    **capsule, "concepts": concepts, "concept_outcomes": outcomes,
                    "_source_receipt": source_receipt,
                    # CODEX AGENT: a valid matching capsule does not make unreadable sibling rows vanish.
                    # Carry one snapshot-level health receipt into the durable v2 prior event.
                    "_store_source_health": dict(store.source_health),
                }
                canonical_caps.append((similarity, normalized))
                prior.update(concepts)
            return prior, canonical_caps, aliases, splits
        except Exception:  # noqa: BLE001 — cross-run read is advisory; a hiccup just yields no priors
            return set(), [], {}, {}

    def _record_cross_run_prior(self, state: RunState, matched: set, prior_caps) -> None:
        """SURFACE (never gate) the cross-run prior: which of the idea's OWN concepts were tried before, in
        which runs, with each matched concept's retained outcome and the explicitly run-level best metric —
        folds into `RunState.cross_run_priors` for the trace/UI. Best-effort; audit-only, so a failure never
        blocks proposing. Every v2 event carries the source-capsule completeness receipt; legacy event fields
        remain additive aliases so historical consumers keep folding.
        `matched` is the idea∩prior overlap already computed by the caller (never the gating grade)."""
        try:
            matched = sorted(matched)
            if not matched:
                return
            # Filter capsules to those actually contributing a matched concept FIRST, THEN cap — so a
            # matching capsule past the top-N is never silently dropped from the receipt (CODEX).
            runs = []
            source_receipts = []
            store_health = None
            for sim, c in prior_caps:
                source = c.get("_source_receipt")
                if not isinstance(source, dict):
                    source = {
                        "concepts_total": None, "concepts_omitted": None,
                        "concepts_complete": False,
                        "concept_outcomes_total": None, "concept_outcomes_omitted": None,
                        "concept_outcomes_complete": False,
                    }
                # CODEX AGENT: source completeness is a denominator over ALL eligible prior capsules. A
                # partial row where the target is not retained may have omitted that target; filtering to
                # matching rows would repeat the false-completeness bug fixed in concept_card.
                source_receipts.append(source)
                if store_health is None and isinstance(c.get("_store_source_health"), dict):
                    store_health = c["_store_source_health"]
                shared = sorted(set(str(x) for x in (c.get("concepts") or [])) & set(matched))
                if not shared:
                    continue
                oc = c.get("concept_outcomes") or {}
                retained_outcomes = {k: oc[k] for k in shared if k in oc}
                run_best = c.get("best_metric")
                runs.append({
                    "run_id": c.get("run_id"),
                    # Legacy aliases remain through the additive v2 transition. New consumers must label
                    # run_best_metric as RUN-level and use matched_concept_outcomes for concept evidence.
                    "best_metric": run_best,
                    "run_best_metric": run_best,
                    "similarity": round(float(sim), 4),
                    "concepts": shared,
                    "matched_concepts": shared,
                    "outcomes": retained_outcomes,
                    "matched_concept_outcomes": [
                        {"concept": concept, "outcome_retained": concept in oc,
                         "outcome": oc.get(concept)}
                        for concept in shared
                    ],
                    "source_receipt": source,
                })
            if not runs:
                return
            runs.sort(key=lambda r: (-r["similarity"], str(r["run_id"])))   # deterministic, most-similar first
            returned_runs = runs[:8]
            partial = sum(
                not receipt.get("concepts_complete")
                or not receipt.get("concept_outcomes_complete")
                for receipt in source_receipts
            )
            unknown = sum(
                receipt.get("concepts_total") is None
                or receipt.get("concept_outcomes_total") is None
                for receipt in source_receipts
            )
            store_health = store_health if isinstance(store_health, dict) else {}
            quarantined = store_health.get("source_rows_quarantined")
            quarantined = (quarantined if isinstance(quarantined, int)
                           and not isinstance(quarantined, bool) and quarantined >= 0 else 0)
            concept_source = {
                "source_complete": partial == 0 and quarantined == 0,
                "partial_capsules": partial,
                "source_unknown_capsules": unknown,
                "source_concepts_omitted": sum(
                    receipt.get("concepts_omitted") or 0 for receipt in source_receipts),
                "source_outcomes_omitted": sum(
                    receipt.get("concept_outcomes_omitted") or 0 for receipt in source_receipts),
                "source_store_complete": quarantined == 0,
                "source_rows_total": int(store_health.get("source_rows_total", 0) or 0),
                "source_rows_quarantined": quarantined,
                "source_malformed_rows": int(store_health.get("source_malformed_rows", 0) or 0),
                "source_invalid_capsule_rows": int(
                    store_health.get("source_invalid_capsule_rows", 0) or 0),
                "source_duplicate_run_rows": int(
                    store_health.get("source_duplicate_run_rows", 0) or 0),
            }
            self.store.append(EV_CROSS_RUN_PRIOR, {
                "v": 2,
                "node_id": max(state.nodes, default=-1) + 1,
                "matched_concepts": matched, "prior_runs": returned_runs,
                "prior_runs_total": len(runs),
                "prior_runs_omitted": len(runs) - len(returned_runs),
                "prior_runs_complete": len(runs) == len(returned_runs),
                "concept_source": concept_source,
                "stance": getattr(self, "_novelty_stance", None)})
        except Exception:  # noqa: BLE001 — audit only, never block proposing
            return

    def _apply_novelty_gate(self, state: RunState, idea: Idea, repropose=None, researcher=None) -> Idea:
        """E1+T5: novelty/dedup gate over fresh proposals, BEFORE any compute is spent.
        Two layers:
        (1) SEMANTIC (T5, ShinkaEvolve `novelty rejection before evaluation`): if the idea TEXT is a
            near-duplicate of an existing node's, reject it — and when a `repropose` callable is
            given, ask the Researcher ONCE more with the duplicate (and its outcome, especially a
            FAILURE) surfaced, so the search learns "you already tried X, it scored Y because Z"
            instead of paying another eval for the same idea.
        (2) NUMERIC (E1 legacy): params within `novelty_epsilon` (normalized L2) of an existing
            node are deterministically nudged off the duplicate.
        Loop-safe (always returns a usable idea) and replay-safe (the final idea lands in
        node_created; the gate is not re-run on replay). Runs when `novelty_gate` is on OR the
        Strategist's novelty stance is "explore" (slice 5): the stance can engage a soft dedup +
        one informed re-propose even when the static gate is off, so novelty pressure follows the
        meta-controller. "balanced"/"exploit" (and gate off) leave this a no-op — exactly as before."""
        # PART IV D3 (Phase 2b): the concept-graph pre-gate runs FIRST. When it recognizes a legitimate
        # same-direction-new-implementation (level 4) or a re-open of a wrongly-abandoned failed direction
        # (level 5), it SHORT-CIRCUITS — the flat gate below can't make that distinction and would wrongly
        # reject the proposal. Every other grade (and the flag being off) falls through UNCHANGED.
        graded = self._graded_novelty_precheck(state, idea)
        if graded is not None:
            # The graded pre-gate is a DELIBERATE short-circuit (Phase-2b, behind `_novelty_mode`): only
            # levels 4/5 return here, and only to ADMIT a proposal the flat gate would wrongly reject (a
            # legitimate same-direction-new-implementation, or the re-open of a wrongly-abandoned failed
            # direction). It never ADMITS a true duplicate: `_graded_novelty_precheck` runs a verbatim-dup
            # guard scanning ALL prior nodes (B1 fix), so a punctuation-only paraphrase can't reach here —
            # it falls through to the stronger `_llm_novelty_gate` below like every other grade.
            return graded
        mode = getattr(self, "_novelty_mode", "llm")
        # "llm" -> an LLM adjudicates duplication by READING the real experiments (not an embedding/
        # distance heuristic), then re-proposes if it's a dup.
        if mode == "llm":
            return self._llm_novelty_gate(state, idea, repropose, researcher=researcher)
        # The deterministic "algo" gate below runs when mode is "algo" OR the Strategist's novelty stance
        # is "explore" (the stance can engage a cheap soft dedup + one informed re-propose even when the
        # mode is otherwise off). "off" without explore leaves this a no-op — the Researcher's own
        # read-the-history judgment stands.
        if not (mode == "algo" or self._novelty_stance == "explore"):
            return idea
        import random as _random

        from looplab.events.digest import numeric_params, param_distance

        if self._novelty_semantic:
            dup, sim = self._semantic_duplicate(state, idea)
            if dup is not None:
                outcome = (f"it FAILED ({dup.error_reason}: {(dup.error or '')[:80]})"
                           if dup.status is NodeStatus.failed
                           else f"it scored {dup.metric}")
                self.store.append(EV_NOVELTY_REJECTED, {
                    # prospective id = max+1, not len() (gap-safe; audit only) — see the llm gate above.
                    "node_id": max(state.nodes, default=-1) + 1, "near_node": dup.id, "kind": "semantic",
                    "similarity": round(sim, 4), "stance": self._novelty_stance,
                    "action": "reproposed" if callable(repropose) else "kept"})
                if callable(repropose):
                    hint = (f"\nNOVELTY GATE: your proposal is a near-duplicate of experiment "
                            f"#{dup.id} ('{self._idea_text(dup.idea)[:160]}') — {outcome}. "
                            "Propose something MEANINGFULLY DIFFERENT (another approach, "
                            "component or direction), not a rewording.")
                    idea = self._repropose_with_feedback(repropose, hint, idea, researcher=researcher)

        params = numeric_params(idea.params)
        if not params:
            return idea

        nearest, mind = None, float("inf")
        for n in state.nodes.values():
            d = param_distance(params, n.idea.params)
            if d < mind:
                mind, nearest = d, n.id
        if mind >= self._novelty_epsilon:
            return idea
        nid = max(state.nodes, default=-1) + 1       # the PROSPECTIVE id (max+1, not len — gap-safe)
        rng = _random.Random(nid * 1009 + 7)        # deterministic per node-slot
        nudged = dict(idea.params)
        for k in params:
            scale = max(abs(params[k]), 1.0) * 0.1
            nudged[k] = round(params[k] + rng.uniform(-1.0, 1.0) * scale, 4)
        self.store.append(EV_NOVELTY_REJECTED, {
            "node_id": nid, "near_node": nearest, "distance": round(mind, 4),
            "stance": self._novelty_stance,
            "original": idea.params, "nudged": nudged})
        out = idea.model_copy()
        out.params = nudged
        out.rationale = (idea.rationale + " [novelty-gate: nudged off a near-duplicate]").strip()
        return out
