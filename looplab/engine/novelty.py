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
from looplab.core.models import NODE_CONCEPT_PROVENANCE_CLASSIFIER, Idea, NodeStatus, RunState
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

    def _llm_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
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
            idea = self._repropose_with_feedback(repropose, hint, idea)
        return idea

    def _repropose_with_feedback(self, repropose, hint: str, idea: Idea) -> Idea:
        """One informed re-propose with the duplicate surfaced as a TRANSIENT `_novelty_feedback`
        directive (shared by the LLM and semantic gates — the set/try/finally-restore discipline
        must stay identical in both). `BudgetExceeded` re-raises (the hard budget stop must end the
        run, not be swallowed); any other repropose failure keeps the original idea. The `finally`
        ALWAYS restores the previous feedback, even if repropose() raised: otherwise this transient
        "you are duplicating #N" directive leaks into EVERY later proposal in the run — including
        drafts in unrelated regions — permanently mis-steering the researcher away from a direction
        the operator never banned."""
        prev = getattr(self.researcher, "_novelty_feedback", "")
        setattr(self.researcher, "_novelty_feedback", hint)
        try:
            idea2 = repropose()
            if idea2 is not None:
                idea = idea2
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — a repropose failure keeps the original idea
            pass
        finally:
            setattr(self.researcher, "_novelty_feedback", prev)
        return idea

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
        # CODEX AGENT: an Idea's concepts are authored by the same proposer whose admission is being
        # decided, so they cannot certify their own graded-novelty bypass. Only replay-proven
        # `node_concepts` classifier events enter the agentic path; missing/unknown provenance fails
        # closed to the curated heuristic path (or no-op when no curated vocabulary exists).
        experiment_ids = {nd.id for nd in _experiment_nodes(state)}
        classifier_ids = {
            nid for nid in experiment_ids
            if nid in all_node_concepts
            and concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_CLASSIFIER
        }
        # CODEX AGENT: a partial cadence is UNKNOWN coverage, not evidence that the remaining nodes
        # touch no concepts. Once any classifier receipt exists, every experiment must have one (an
        # explicit empty list is valid) before this precheck may issue an L4/L5 admission override.
        # Otherwise a post-cadence near-repeat or win could disappear from the grade; defer to the
        # ordinary flat gate without mixing proposer-authored labels into the trusted channel.
        if classifier_ids and classifier_ids != experiment_ids:
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
            from looplab.engine.memory import ConceptCapsuleStore
            # NOTE (full-CR TODO, §21.20.13 CR2a): this reloads+scans the whole capsule JSONL per proposal;
            # a bounded, versioned, scope-keyed query index replaces it once the retrieval planner lands.
            store = ConceptCapsuleStore(Path(self.memory_dir) / "concept_capsules.jsonl")
            fp = self.lessons.task_fingerprint(state, state.best())
            # HARD direction gate (CODEX): a min/rmse task and a max/recall task can share enough goal
            # tokens to clear the fuzzy Jaccard floor, but their outcomes are not comparable — require the
            # same optimization direction before a prior counts (a lean stand-in for the full contract).
            my_dir = str(getattr(state, "direction", "") or "min")
            caps = [(s, c) for s, c in store.prior_capsules(
                        fp, min_sim=self._CROSS_RUN_MIN_SIM, exclude_run_id=getattr(state, "run_id", "") or "")
                    if str(c.get("direction") or "min") == my_dir]
            aliases = load_concept_aliases(self.memory_dir)
            splits = load_concept_splits(self.memory_dir)
            prior: set[str] = set()
            canonical_caps = []
            for similarity, capsule in caps:
                raw = [str(x) for x in (capsule.get("concepts") or []) if str(x)]
                concepts = canonicalize_concepts(raw, aliases=aliases, splits=splits)
                if not concepts:
                    continue
                outcomes = {}
                raw_outcomes = capsule.get("concept_outcomes") or {}
                if isinstance(raw_outcomes, dict):
                    for source in sorted(raw_outcomes, key=str):
                        target = canonicalize_concept(source, sibling_concepts=raw,
                                                      aliases=aliases, splits=splits)
                        if target and target in concepts and target not in outcomes:
                            outcomes[target] = raw_outcomes[source]
                normalized = {**capsule, "concepts": concepts, "concept_outcomes": outcomes}
                canonical_caps.append((similarity, normalized))
                prior.update(concepts)
            return prior, canonical_caps, aliases, splits
        except Exception:  # noqa: BLE001 — cross-run read is advisory; a hiccup just yields no priors
            return set(), [], {}, {}

    def _record_cross_run_prior(self, state: RunState, matched: set, prior_caps) -> None:
        """SURFACE (never gate) the cross-run prior: which of the idea's OWN concepts were tried before, in
        which runs, with the best outcome each — folds into `RunState.cross_run_priors` for the trace/UI
        ('tried in run X -> metric Y'). Best-effort; audit-only, so a failure never blocks proposing.
        `matched` is the idea∩prior overlap already computed by the caller (never the gating grade)."""
        try:
            matched = sorted(matched)
            if not matched:
                return
            # Filter capsules to those actually contributing a matched concept FIRST, THEN cap — so a
            # matching capsule past the top-N is never silently dropped from the receipt (CODEX).
            runs = []
            for sim, c in prior_caps:
                shared = sorted(set(str(x) for x in (c.get("concepts") or [])) & set(matched))
                if not shared:
                    continue
                oc = c.get("concept_outcomes") or {}
                runs.append({"run_id": c.get("run_id"), "best_metric": c.get("best_metric"),
                             "similarity": round(float(sim), 4),
                             "concepts": shared, "outcomes": {k: oc[k] for k in shared if k in oc}})
            if not runs:
                return
            runs.sort(key=lambda r: (-r["similarity"], str(r["run_id"])))   # deterministic, most-similar first
            self.store.append(EV_CROSS_RUN_PRIOR, {
                "node_id": max(state.nodes, default=-1) + 1,
                "matched_concepts": matched, "prior_runs": runs[:8],
                "stance": getattr(self, "_novelty_stance", None)})
        except Exception:  # noqa: BLE001 — audit only, never block proposing
            return

    def _apply_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
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
            return self._llm_novelty_gate(state, idea, repropose)
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
                    idea = self._repropose_with_feedback(repropose, hint, idea)

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
