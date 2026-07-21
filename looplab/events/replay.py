"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

import heapq
import math
from typing import Iterable, Literal, Optional

from looplab.core.concepts import (
    CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
    CONCEPT_DELTA_MISSING_RUN_BASE_REASON,
    CONCEPT_DELTA_MISSING_PARENT_REASON,
    CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON,
    CONCEPT_INVALID_ID_REASON,
    CONCEPTS_PER_NODE_CAP_REASON,
    CONCEPT_MODE_UNSUPPORTED_REASON,
    ConceptMaterializationReason,
    BoundedConceptAccumulator,
    bounded_raw_concept_values,
    concept_materialization_receipt,
    normalized_concept_materialization_receipt,
    normalized_concept_renames,
    resolve_concept_set_reasons,
)
from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, SearchFitness, is_usable_metric,
                                  verifier_evidence_digest)
from looplab.core.models import (CARD_ACTION_DIGEST_V1_FIELDS, NODE_CONCEPT_PROVENANCE_AUTHORED,
                     NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR,
                     NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                     NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                     node_concept_event_provenance,
                     Card, CardConceptSource, CardIdentityProvenance, CardSelectionProvenance,
                     Event, Hypothesis, Idea, Node, NodeStatus, RunState, Trial, card_action_digest,
                     card_ownership_receipt, hypothesis_id, hypothesis_statement_digest,
                     idea_proposal_digest, normalize_extra_metrics, normalize_researcher_footprint,
                     run_setup_key, valid_researcher_footprint)
from looplab.events.comment_projection import apply_comment_event
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_ANNOTATION, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_BUDGET_EXTEND, EV_CONFIRM_DONE,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_CONFIRM_EVAL, EV_DATA_LEAKAGE, EV_DATA_PROFILED, EV_DATA_PROVENANCE, EV_ENV_CHANGED,
    EV_CONCEPT_COVERAGE_SNAPSHOT, EV_COVERAGE_SNAPSHOT, EV_DEEP_RESEARCH, EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM,
    EV_CARD_ADDED, EV_CARD_DROPPED, EV_CARD_ENRICHED, EV_CARD_MERGED, EV_CARD_RANKED,
    EV_FORESIGHT_SELECTED, EV_FORK,
    EV_FORK_DONE, EV_HINT, EV_HOLDOUT_EVALUATED, EV_HOST_GRADING, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
    EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED, EV_INJECT_DONE, EV_INJECT_NODE, EV_LESSONS_DISTILLED,
    EV_LESSONS_REFRESHED, EV_LLM_COST, EV_LLM_USAGE, EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_CONCEPT_CONSOLIDATION, EV_CONCEPT_EDGE, EV_CONCEPT_TAG_EDITED,
    EV_HYPOTHESIS_CONCEPTS, EV_NODE_CONCEPTS, EV_RUN_CONCEPTS,
    EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED, EV_NODE_RESET,
    EV_CROSS_RUN_PRIOR,
    EV_NODE_TOMBSTONED, EV_NODE_VERIFIED, EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED, EV_PAUSE, EV_STAGE_FINISHED,
    EV_POLICY_DECISION, EV_PROMOTE, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_COMPLETED, EV_RESTART, EV_RESUME, EV_RESUME_REQUESTED, EV_RESUME_SERVED,
    EV_REWARD_HACK_SUSPECTED, EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_SETUP_FINISHED, EV_RUN_STARTED, EV_RUNG_PROMOTED,
    EV_SET_STRATEGY,
    EV_SETUP_FINISHED, EV_SPEC_APPROVAL_REQUESTED, EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED,
    EV_STRATEGY_DECISION, EV_TRUST_GATE_CHANGED, EV_VERIFIER_GROUP_SCORED, EV_WORKSPACE_CHANGED)


def flagged_node_ids(st: RunState) -> set:
    """T2: node ids excluded from best/holdout selection under trust_gate gate/block — those with a
    HIGH-PRECISION cheating/leakage signal. The heuristic `critic:` and `perfect_metric` signals
    stay advisory in every mode (perfect_metric flags metric<=0 (min) / >=1 (max), which
    legitimately-perfect scores hit, so gating on it could exclude honest winners). Empty under
    `audit`. Shared by the fold and the engine's holdout-topk so both apply the SAME exclusion."""
    if st.trust_gate not in ("gate", "block"):
        return set()
    return hard_flagged_ids(st)


def promotion_eligible_nodes(st: RunState, *, flagged=None) -> list[Node]:
    """Nodes allowed to publish selection-affecting or promoted cross-run measurements."""
    excluded = flagged_node_ids(st) if flagged is None else set(flagged)
    return [node for node in st.evaluated_nodes()
            if SearchFitness.eligible(node, excluded, st.aborted_nodes)]


def verifier_tie_groups(st: RunState, *, holdout_select: bool | None = None,
                        ci_tie: bool | None = None) -> list[list[Node]]:
    """Return the one complete tie-set that can affect the selector's final answer.

    Holdout promotion runs last.  Once it has a non-empty eligible pool, no mean/CI decision can reach the
    final champion, so surfacing both groups wastes calls and can leave incomparable overlapping treatments.
    Without a holdout pool, mirror the mean selector's confirmed-pool and CI/exact tie semantics.
    """
    holdout_select = st.holdout_select if holdout_select is None else bool(holdout_select)
    ci_tie = st.verifier_ci_tie if ci_tie is None else bool(ci_tie)
    eligible = promotion_eligible_nodes(st)
    confirmed = [n for n in eligible if n.confirmed_mean is not None]
    pool = confirmed if confirmed else eligible
    def _champion_tie(nodes, metric_of):
        candidates = [n for n in nodes if metric_of(n) is not None]
        if not candidates:
            return []
        chooser = min if st.direction == "min" else max
        leader = chooser(candidates, key=lambda n: (metric_of(n), n.id))
        return [n for n in candidates if metric_of(n) == metric_of(leader)]

    holdout_pool = [n for n in eligible if is_usable_metric(n.holdout_metric)]
    if holdout_select and holdout_pool:
        tied = _champion_tie(holdout_pool, lambda n: n.holdout_metric)
    elif ci_tie:
        tied = SearchFitness(st.direction, verifier_tiebreak=True, ci_tie=True).ci_tie_set(pool)
    else:
        tied = _champion_tie(pool, lambda n: n.robust_metric)
    return [sorted(tied, key=lambda n: n.id)] if (
        len(tied) >= 2 and any(node.verifier_score is None for node in tied)) else []


def is_hard_signal(sig: str) -> bool:
    """Is this reward-hack/leakage signal HIGH-PRECISION (gating + agent-facing), vs advisory noise?

    The single classifier shared by `hard_flagged_ids` (gate/block selection exclusion) AND
    `digest.trust_reflection._sigs` (which signals to NAME in the agent hint) — kept here so the two
    can't drift: before, `_sigs` stripped EVERY `critic:` signal while `hard_flagged_ids` promoted
    `critic:hardcoded_metric`, so a node hard-flagged ONLY for that rendered as "node N ()" (a
    contentless warning). `critic:hardcoded_metric` is HIGH-PRECISION (the critic requires a LITERAL
    metric value with no computed assignment anywhere), so it gates — closing the "hardcode a
    near-optimal metric and win under every built-in gate" bypass on self-report tasks. Other
    `critic:` issues and `perfect_metric` (which a legitimately-perfect score hits) stay advisory."""
    sig = str(sig)
    if sig == "critic:hardcoded_metric":
        return True
    # `protected_audit_unavailable` (the whole workdir-tamper audit threw) is fail-closed evidence
    # that the node is NOT verified-clean, but it is not itself proof of tampering — a transient FS
    # error should SURFACE to the operator/agent, not gate-exclude an honest node. So it stays
    # advisory alongside critic:*/perfect_metric. `protected_missing`/`protected_unreadable` (a
    # protected file we placed is gone/corrupt) ARE real tamper evidence and remain HARD (P1-6).
    # `suspicious_output` is a broad SHAPE heuristic (the `looplab harden` constant-prediction rule,
    # pattern `[x]*NNN`) that also matches ordinary buffer pre-allocation (`weights = [0]*1000`); a
    # constant predictor already loses on ground truth, so hard-gating it only risks silently excluding
    # an HONEST winner. Advisory (surface, never gate), exactly like perfect_metric.
    return not sig.startswith(("critic:", "perfect_metric", "protected_audit_unavailable",
                               "suspicious_output"))


def hard_flagged_ids(st: RunState) -> set:
    """Node ids carrying a HIGH-PRECISION (non-`critic:`, non-`perfect_metric`) cheating/leakage
    signal, INDEPENDENT of `trust_gate` mode. `flagged_node_ids` uses it for gate/block selection
    exclusion; the agent-facing trust-reflection hint (signal-delivery §1) uses it to warn the
    Researcher about a flagged lineage even under `audit`, where nothing is gate-excluded."""
    def _has_current_hard_signal(rh: dict) -> bool:
        nid = _coerce_node_id(rh)
        n = st.nodes.get(nid) if nid is not None else None
        if n is None or rh.get("generation", n.attempt) != n.attempt:
            return False
        return any(is_hard_signal(s.get("signal", "")) for s in (rh.get("signals") or []))
    return {nid for r in st.reward_hacks
            if _has_current_hard_signal(r) and (nid := _coerce_node_id(r)) is not None}


# --------------------------------------------------------------------------- fold dispatch
# One handler per event type (docs/15 §P5.1): the bodies below are the VERBATIM arms of the
# former 63-way if/elif chain, one function each, dedented — with exactly three mechanical
# adjustments, all noted in place: (a) `continue` became `return` in _on_node_created (same
# meaning: skip the rest of THIS event); (b) the EV_BEST_CONFIRMED arm writes the fold-local
# through `ctx` (the ONE cross-arm value, threaded explicitly instead of a closure variable);
# (c) the resume/reopen twin arm is ONE handler registered under both keys.
# Every handler is a pure `(st, e, d, ctx) -> None` mutation — no I/O, no LLM calls — invoked
# in log order by `fold`, so determinism/order-tolerance are structurally unchanged; unknown
# event types still no-op via `_HANDLERS.get`. The uniform signature keeps the registry
# mechanical; most handlers ignore `e`/`ctx`.


class _FoldCtx:
    """Cross-arm state for selection, accounting de-dup, and finish/report adjacency."""
    __slots__ = (
        "best_confirmed", "best_confirmed_significant", "llm_usage_seen", "llm_usage_ids",
        "charged_terminal_generations", "charged_confirm_seeds", "charged_ablation_ids",
        "pending_finish_report", "concept_subject_invalidated", "concept_mode_untrusted",
        "concept_input_capped", "concept_input_invalid", "run_base_capped",
        "run_base_invalid", "run_base_seen", "event_index",
    )

    def __init__(self):
        self.best_confirmed: int | None = None
        # R1-d: whether the confirm certificate found a SIGNIFICANT winner. Only consulted when a
        # best_confirmed is set; defaults True so legacy events / the ci_tie-off path keep the unconditional
        # override (byte-identical). A non-significant confirm under `verifier_ci_tie` must NOT erase best_ci.
        self.best_confirmed_significant: bool = True
        # Legacy summaries are last-write-wins only until the durable delta ledger begins.
        self.llm_usage_seen = False
        # New ledgers retry an ambiguously acknowledged append with the same identity. Replay is
        # first-write-wins for that ID; legacy usage events without an ID remain additive.
        self.llm_usage_ids: set[str] = set()
        # First terminal COST wins per (node,lifecycle), independently from whether that lifecycle is
        # still current. A reset may discard its metric/state, but cannot refund compute already spent.
        self.charged_terminal_generations: set[tuple[int, int]] = set()
        self.charged_confirm_seeds: set[tuple[int, int, int]] = set()
        self.charged_ablation_ids: set[str] = set()
        # (physical event seq, physical fold index, content). The index is needed for legacy logs
        # whose envelopes have no meaningful seq but whose report->finish adjacency is still valid.
        self.pending_finish_report: tuple[int, int, dict] | None = None
        # Fold-only receipt boundary for legacy, unstamped node_concepts events. Lifecycle attempts also
        # advance for eval/code retries, but concept evidence becomes ambiguous only after the IDEA changed.
        self.concept_subject_invalidated: set[int] = set()
        # Explicit future/malformed mode values are not legacy absence. Keep the node, but make its
        # concept membership unavailable until a reviewed mode or independent classifier supersedes it.
        self.concept_mode_untrusted: set[int] = set()
        self.concept_input_capped: set[int] = set()
        self.concept_input_invalid: set[int] = set()
        self.run_base_capped = False
        self.run_base_invalid = False
        # A zero-length base is valid and distinct from no base event. Delta roots need this fold-only
        # presence bit because RunState.run_base_concepts alone represents both states as ``[]``.
        self.run_base_seen = False
        self.event_index = -1

def _on_run_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Read with defaults like every other fold handler (RunState already defaults these to ""): the
    # fold loop dispatches handlers with NO per-event try/except, so a bare d["run_id"] KeyError on a
    # malformed/hand-edited run_started would take down the WHOLE fold (every view/replay/resume of the
    # run) — the exact hand-edited-log-tolerance the _on_node_created guard was added to provide.
    st.run_id = d.get("run_id", "")
    st.task_id = d.get("task_id", "")
    st.goal = d.get("goal", "")
    # `direction` drives is_better/best-selection for the whole run — a typo ("Max",
    # "maximize") must not silently invert the objective. Accept only the two valid values;
    # anything else falls back to the safe default rather than flipping optimization.
    _dir = str(d.get("direction", "min")).strip().lower()
    st.direction = _dir if _dir in ("min", "max") else "min"
    st.config_hash = d.get("config_hash", "")
    st.workspace = d.get("workspace")
    st.env = d.get("env")   # P0-5 environment identity pinned at start (None on old logs)
    _di = d.get("dirty_inputs")
    st.dirty_inputs = _di if isinstance(_di, list) else []   # P0-5 uncommitted-input enumeration
    _tg = str(d.get("trust_gate", "audit")).strip().lower()
    st.trust_gate = _tg if _tg in ("audit", "gate", "block") else "audit"
    # D1: recorded at start so replay applies the same selection rule. Absent in old
    # logs -> False -> byte-identical legacy selection.
    st.holdout_select = bool(d.get("holdout_select", False))
    # The reserved-holdout fraction the run committed to (the split every search metric was
    # scored against). None in old logs; the engine re-uses it on resume so a changed live
    # setting can't make pre/post-resume metrics incomparable.
    _hf = d.get("holdout_fraction")
    st.holdout_fraction = float(_hf) if is_usable_metric(_hf) else None
    # R1-c: recorded at start so replay applies the same selection rule (config isn't available to the
    # pure fold). Absent in old logs -> False -> byte-identical legacy selection.
    # The fold stays pinned to the RECORDED value (never a live re-read); the engine re-pins its own
    # `_select_verifier` gate from this recorded value on resume (orchestrator `_reentry_repin`), so the
    # fold's tie-break rule and the live verify production can't diverge across a config edit (invariant #6).
    st.select_verifier_tiebreak = bool(d.get("select_verifier", False))
    st.verifier_ci_tie = bool(d.get("verifier_ci_tie", False))   # R1-d: absent on old logs -> exact-tie
    samples = d.get("select_verifier_samples", 3)
    st.select_verifier_samples = (samples if isinstance(samples, int) and not isinstance(samples, bool)
                                  and 1 <= samples <= 32 else 3)
    contract = d.get("select_verifier_contract", VERIFIER_SELECTION_CONTRACT)
    st.select_verifier_contract = (contract if isinstance(contract, str) and len(contract) <= 80
                                   else VERIFIER_SELECTION_CONTRACT)

def _on_trust_gate_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Operator edited the run's trust gate after launch (server config edit). Last write
    # wins so the change engages in every fold — live view, resume, reset — immediately.
    _tg = str(d.get("trust_gate", "")).strip().lower()
    if _tg in ("audit", "gate", "block"):
        st.trust_gate = _tg

def _on_node_building(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Transient "a node is being built RIGHT NOW" marker (see EV_NODE_BUILDING docs): show it in
    # the UI the instant work starts, before node_created. NOT added to st.nodes, so id
    # allocation + resume are untouched. Superseded/cleared by this node's node_created below.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    current = st.nodes.get(nid)
    if current is not None and (nid in st.aborted_nodes or current.tombstoned):
        _clear_build_marker(st, d, nid)
        return
    if current is not None and not _generation_matches(current, d):
        return
    marker = {"node_id": nid, "operator": d.get("operator"),
              "parent_ids": d.get("parent_ids", []), "started": e.ts}
    generation = _event_generation(d)
    if generation is not _MISSING:
        marker["generation"] = generation
    # Set BOTH the singular back-compat marker and this node's entry in the multi-build collection
    # (same dict object). A concurrent sibling's node_building overwrites `st.building` (last wins) but
    # only its OWN `st.buildings` key, so every in-flight build survives in the collection.
    st.building = marker
    st.buildings[nid] = marker

def _on_node_created(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Don't let a duplicate node_created RESURRECT a settled node (invariant #2 "first terminal
    # wins"): if the id already exists AND is in a TERMINAL state (evaluated/failed), skip the event.
    # Overwriting a terminal node installed a fresh status=pending Node, which re-armed the
    # `first_terminal` guard so a following duplicate terminal RE-added its eval_seconds to
    # total_eval_seconds (cost double-charged) and could flip a settled metric/status/feasibility
    # last-wins — the exact idempotency `_on_node_evaluated` protects the terminal against.
    # A re-emit onto a PENDING id is legitimate and MUST apply: `node_reset` (propose/implement)
    # re-opens a node to pending and the engine re-develops it in place, emitting a SECOND
    # node_created for the same id (orchestrator `_rerun_reset_node`) whose new code/idea must land
    # and clear `rerun_from` — dropping it loops the engine forever re-developing. So the guard keys
    # on terminal status, not mere existence. A clean first build has no prior node -> applies.
    # Coerce BEFORE looking up the settled lifecycle. A numeric-string duplicate ("0") names the
    # same node as integer 0 and must not bypass first-terminal-wins by missing the raw dict key.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    existing = st.nodes.get(nid)
    if existing is not None and existing.status is not NodeStatus.pending:
        return
    # Defensive like the per-trial / unknown-node tolerance below: a malformed or incomplete
    # node_created (missing key, non-coercible idea param in a hand-edited / bring-your-own-script
    # log) must not crash the WHOLE fold — skip the bad event instead (the engine, sole writer,
    # always round-trips a validated Idea, so this only fires on a corrupt log).
    if not _parent_generation_map_matches(st, d):
        _clear_build_marker(st, d, nid)
        return
    current = st.nodes.get(nid)
    if current is not None and (nid in st.aborted_nodes or current.tombstoned):
        _clear_build_marker(st, d, nid)
        return
    generation = _event_generation(d)
    if generation is _MISSING:
        # Old node_created records were unstamped. On an initial create their generation is zero;
        # on a legacy in-place rebuild preserve the generation the preceding node_reset established.
        generation = current.attempt if current is not None else 0
    if generation is None or generation < 0:
        return
    if current is not None and generation != current.attempt:
        return                       # a late rebuild from a superseded lifecycle
    try:
        n = Node(
            id=nid,
            parent_ids=d.get("parent_ids", []),
            operator=d["operator"],
            idea=Idea(**d["idea"]),
            code=d.get("code", ""),
            files=d.get("files", {}) or {},
            deleted=d.get("deleted", []) or [],
            attempt=generation,
            origin=d.get("origin"),   # cross-run provenance (None for ordinary nodes)
            research_origin=d.get("research_origin"),   # 💡 proposed just after a deep-research memo
        )
    except (MemoryError, RecursionError):
        # A RESOURCE glitch is NOT a corrupt-data error: it must fail LOUD, not be swallowed.
        # A MemoryError silently caught here drops the node -> fold returns empty nodes ->
        # `_create_node` re-computes node_id=0 forever -> a 184MB node_created(0) runaway. Let
        # it propagate so a transient glitch surfaces instead of self-sustaining into a spin.
        raise
    except Exception:
        return   # (was `continue` in the loop arm: skip just this event)
    raw_idea = d.get("idea") if isinstance(d.get("idea"), dict) else {}
    raw_concept_receipts = {
        field: bounded_raw_concept_values(raw_idea[field])
        for field in ("concepts", "concepts_added", "concepts_removed") if field in raw_idea
    }
    delta_added = [str(c) for c in (getattr(n.idea, "concepts_added", None) or [])]
    delta_removed = [str(c) for c in (getattr(n.idea, "concepts_removed", None) or [])]
    mode_present = "concept_mode" in raw_idea
    raw_mode = raw_idea.get("concept_mode")
    recognized_mode = raw_mode if isinstance(raw_mode, str) and raw_mode in ("full", "delta") else None
    unsupported_mode = mode_present and recognized_mode is None
    if unsupported_mode:
        # CODEX AGENT: forward compatibility belongs at the node boundary. Keep the experiment and its
        # audit Idea, but never guess how a future/malformed envelope changes membership.
        ctx.concept_mode_untrusted.add(n.id)
    else:
        ctx.concept_mode_untrusted.discard(n.id)
    delta_mode = recognized_mode == "delta"
    raw_transitional_delta = any(
        isinstance(raw_idea.get(field), list) and bool(raw_idea.get(field))
        for field in ("concepts_added", "concepts_removed")
    )
    if not mode_present and raw_transitional_delta:
        # CODEX AGENT: 40a5a94 briefly wrote non-empty delta lists before the discriminator existed.
        # Preserve those durable rows, but canonicalize the replayed Idea to explicit `delta` so a
        # subsequent dump round-trips the semantic choice. Modern zero-deltas rely only on the mode.
        delta_mode = True
        n.idea.concept_mode = "delta"
    authoritative_fields = (
        tuple(raw_concept_receipts)
        if unsupported_mode else
        ("concepts_added", "concepts_removed") if delta_mode else ("concepts",)
    )
    authoritative_receipts = [raw_concept_receipts[field] for field in authoritative_fields
                              if field in raw_concept_receipts]
    input_capped = any(overflow for _values, overflow, _invalid in authoritative_receipts)
    input_invalid = any(invalid for _values, _overflow, invalid in authoritative_receipts)
    current_provenance = st.node_concept_provenance.get(n.id)
    concept_subject_unchanged = bool(
        current is not None
        and current.operator == n.operator
        # The independent tagger reads none of the proposer-authored concept envelope. Excluding every
        # such field preserves an existing evidence receipt when only the proposer's taxonomy changes.
        and current.idea.model_dump(exclude={"concept_mode", "concepts", "concepts_added",
                                             "concepts_removed"})
        == n.idea.model_dump(exclude={"concept_mode", "concepts", "concepts_added",
                                      "concepts_removed"})
    )
    # A same-idea re-emission (an implement/eval reset re-emits node_created for the UNCHANGED idea) must
    # NOT downgrade an existing independent CLASSIFIER receipt, an operator's deliberate OPERATOR edit,
    # or a persisted OFFLINE display receipt — all describe the unchanged idea and stand. Only a subject
    # CHANGE (a propose reset already cleared the receipt) or a fresh tag event supersedes them. The offline
    # receipt remains non-evidence and is excluded from the cadence's known-tag cache, so the next classifier
    # pass upgrades it rather than treating the coarse result as complete.
    receipt_protected = bool(concept_subject_unchanged and current_provenance in (
        NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR,
        NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC))
    if receipt_protected:
        # The independent/operator full set owns the membership. A same-subject re-emission may retain
        # a malformed proposer envelope for audit, but it must not poison the protected classification.
        ctx.concept_mode_untrusted.discard(n.id)
    else:
        if input_capped:
            ctx.concept_input_capped.add(n.id)
        else:
            ctx.concept_input_capped.discard(n.id)
        if input_invalid:
            ctx.concept_input_invalid.add(n.id)
        else:
            ctx.concept_input_invalid.discard(n.id)
    st.nodes[n.id] = n
    # Researcher-AUTHORED concepts populate the compatible concept read model at creation, but the
    # provenance sidecar prevents an admission consumer from mistaking that self-authored taxonomy for
    # independent classifier evidence. A later `node_concepts` event overrides both, last-write-wins.
    if current is not None and not concept_subject_unchanged:
        # CODEX AGENT: a replacement node_created is a new tagging subject even if a malformed writer
        # skipped the propose reset. Clear every old receipt symmetrically: an authored mapping is just
        # as stale as a classifier/operator mapping when the replacement Idea carries no concepts of its own.
        st.node_concepts.pop(n.id, None)
        st.node_concept_provenance.pop(n.id, None)
        st.node_concepts_at_vocab.pop(n.id, None)
        st.node_concept_deltas.pop(n.id, None)
        ctx.concept_subject_invalidated.add(n.id)
    if delta_mode and not unsupported_mode and not receipt_protected:
        # PART V (B): the node authored a DELTA vs the run base + its parents. Store the tolerant reader's
        # bounded valid operands here; the append-only Event remains the lossless audit source. The fold
        # post-pass (`_materialize_concept_deltas`) resolves node_concepts topologically over the complete
        # DAG, so fold stays order-tolerant. Provenance stays `authored` so a classifier/operator event
        # still wins (the post-pass fills only nodes that keep the authored delta). Empty lists are an
        # explicit zero delta, so they still create a sidecar and materialized membership.
        st.node_concept_deltas[n.id] = {"added": delta_added, "removed": delta_removed}
        st.node_concept_provenance[n.id] = NODE_CONCEPT_PROVENANCE_AUTHORED
        st.node_concepts_at_vocab.pop(n.id, None)
    elif (not unsupported_mode and not receipt_protected
          and (n.idea.concepts or recognized_mode == "full")):
        # Full is an exact replacement. An explicit `full` + [] is therefore a known-empty membership,
        # while an old no-mode/no-concepts payload stays genuinely absent for replay compatibility.
        st.node_concept_deltas.pop(n.id, None)
        st.node_concepts[n.id] = [str(c) for c in n.idea.concepts]
        st.node_concept_provenance[n.id] = NODE_CONCEPT_PROVENANCE_AUTHORED
        st.node_concepts_at_vocab.pop(n.id, None)
    elif not receipt_protected:
        # Unknown mode and genuinely absent legacy membership are both non-authoritative. A pending
        # replacement must not retain a previous authored set merely because classifier-protected
        # subject equality intentionally ignores the proposer concept envelope.
        st.node_concept_deltas.pop(n.id, None)
        if st.node_concept_provenance.get(n.id) == NODE_CONCEPT_PROVENANCE_AUTHORED:
            st.node_concepts.pop(n.id, None)
            st.node_concept_provenance.pop(n.id, None)
            st.node_concepts_at_vocab.pop(n.id, None)
    if current is None:
        # A holdout score is a disclosed final-exam signal. If a genuinely NEW candidate lands
        # afterwards (an inject/fork/policy action won the finish CAS race), the search has become
        # adaptive to that signal. Rotate the hidden split before any later promotion can reuse it.
        _invalidate_disclosed_holdout(st, fresh_node_ids={n.id})
        # A genuinely new candidate invalidates any confirmation/approval completed for the prior
        # candidate set — including when it is created just AFTER best_confirmed was appended.
        ctx.best_confirmed = None
        st.confirmed_done = False
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
    _clear_build_marker(st, d, n.id)   # the real node is here now — drop the "building" marker(s)

def _nonneg_seconds(v) -> float:
    """Coerce a PERSISTED eval-cost value to a FINITE, NON-NEGATIVE float before it enters the
    cumulative budget. A hand-edited / foreign-writer log with eval_seconds="3" (str) would otherwise
    TypeError the WHOLE fold — taking down every view/replay/resume of the run — and a negative value
    would silently REDUCE total_eval_seconds, extending the budget (arch-review §5 P2). Normal engine
    emitters always produce a clean non-negative float, so this only guards malformed input."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if (math.isfinite(f) and f >= 0.0) else 0.0


def _finite_metric(value):
    """Normalize one selection-affecting persisted scalar without accepting strings or bools."""
    return float(value) if is_usable_metric(value) else None


def _charge_eval_seconds(st: RunState, kind: str, raw) -> None:
    """P1-2 budget buckets: add a coerced non-negative eval-seconds to the cumulative total AND to its
    category bucket (node|confirm). One helper so the total and the per-kind split can never drift."""
    secs = _nonneg_seconds(raw)
    st.total_eval_seconds += secs
    if secs:
        st.eval_seconds_by_kind[kind] = st.eval_seconds_by_kind.get(kind, 0.0) + secs


def _attempt_matches(n, d: dict) -> bool:
    """P0-1 attempt guard: a node terminal (node_evaluated/node_failed) is honored only if the
    `attempt` it was stamped with still matches the node's current attempt generation. `node_reset`
    bumps `n.attempt`, so a LATE terminal from an abandoned attempt (its eval was in flight when the
    reset happened) carries the OLD attempt and is dropped — it can't land as first-terminal-after-
    reset and accept a metric from discarded code (the real compute is still charged separately).
    Truly unstamped terminals predate reset generations and are accepted only for generation 0."""
    generation = _event_generation(d, legacy_attempt=True)
    # Unstamped terminals are legacy generation-0 records. Accepting one after reset would let a
    # delayed old writer impersonate the current lifecycle (ABA); all modern emitters are stamped.
    if generation is _MISSING:
        return n.attempt == 0
    return generation is not None and generation == n.attempt


def _coerce_node_id(d: dict, key: str = "node_id"):
    """Coerce a raw event `node_id` to an int for a fold KEY/membership op, or None if it isn't a usable
    node id. Several sanctioned /control events (`approval_granted`, `annotation`) are appended VERBATIM,
    so a forged `{"node_id":[999]}` (unhashable) / bool / non-numeric id must be rejected BEFORE it
    reaches a dict/set hash — else the fold raises `TypeError: unhashable` and bricks every replay. Rejects
    a bool (subclasses int, so int(True)==1 would spuriously match node 1) and anything non-coercible
    (incl. a non-finite float -> OverflowError). A missing/None id also returns None; each handler decides
    whether that means accept (a bare grant) or drop."""
    v = d.get(key)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # Never truncate 3.9 into node 3 at an approval/control boundary. JSON frontends may
        # legitimately encode an integer as 3.0, so accept only finite integral floats.
        return int(v) if math.isfinite(v) and v.is_integer() else None
    if not isinstance(v, str):
        return None
    try:
        return int(v.strip())
    except (TypeError, ValueError, OverflowError):
        return None


_MISSING = object()


def _event_generation(d: dict, *, legacy_attempt: bool = False):
    """Return an explicitly stamped lifecycle generation, `_MISSING` for a legacy unstamped event,
    or None for an invalid stamp. `node_repaired.data.attempt` predates lifecycle generations and is
    the INLINE-REPAIR ordinal, so callers opt into the terminal-only `attempt` compatibility alias."""
    if "generation" in d:
        raw = d.get("generation")
    elif legacy_attempt and "attempt" in d:
        raw = d.get("attempt")
    else:
        return _MISSING
    generation = _coerce_node_id({"node_id": raw})
    return generation if generation is not None and generation >= 0 else None


def _marker_matches_event(marker: Optional[dict], d: dict, nid: int) -> bool:
    """Core generation guard shared by the singular `st.building` and each per-node `st.buildings`
    entry: only let an event clear the transient marker for the SAME node lifecycle.

    Reruns reuse node ids. A late generation-1 failure must not erase a generation-2 build marker.
    Historical markers were unstamped, so they retain the legacy id-only clear behaviour.
    """
    if not marker or marker.get("node_id") != nid:
        return False
    marker_generation = _event_generation(marker)
    if marker_generation is _MISSING:
        return True
    event_generation = _event_generation(d, legacy_attempt=True)
    return (event_generation is not _MISSING and event_generation is not None
            and event_generation == marker_generation)


def _building_matches_event(st: RunState, d: dict, nid: int) -> bool:
    """Whether `d` clears the SINGULAR back-compat `st.building` marker for `nid`
    (see `_marker_matches_event`)."""
    return _marker_matches_event(st.building, d, nid)


def _clear_build_marker(st: RunState, d: dict, nid: int) -> None:
    """Clear the transient build marker for `nid` on ITS OWN created/terminal/reset/abort event —
    BOTH the singular `st.building` (last concurrent build; back-compat) and the per-node
    `st.buildings` entry, each gated on its own generation. Under `parallel_build>1` the singular
    field holds only the last-appended build, so an EARLIER concurrent build's terminal matches its
    `st.buildings` entry but NOT the singular; keying each off its own marker is exactly what stops
    that entry from leaking a stale breathing 'building…' ghost."""
    if _building_matches_event(st, d, nid):
        st.building = None
    if _marker_matches_event(st.buildings.get(nid), d, nid):
        st.buildings.pop(nid, None)


def _generation_matches(n: Node, d: dict, *, legacy_attempt: bool = False) -> bool:
    generation = _event_generation(d, legacy_attempt=legacy_attempt)
    return generation is _MISSING or (generation is not None and generation == n.attempt)


def _control_generation_matches(n: Node, d: dict) -> bool:
    """Match a lifecycle-mutating operator intent while preserving old persisted logs.

    Historical controls were unstamped and can legitimately contain several resets, so a missing
    stamp binds to the lifecycle visible at that point in the append-only replay. Modern producers
    always stamp and the HTTP boundary performs CAS before append; an explicit stale stamp is rejected.
    """
    generation = _event_generation(d)
    if generation is _MISSING:
        return True
    return generation is not None and generation == n.attempt


def _node_for_event(st: RunState, d: dict) -> Node | None:
    nid = _coerce_node_id(d)
    return st.nodes.get(nid) if nid is not None else None


def _generation_map_matches(st: RunState, d: dict) -> bool:
    """Validate the whole candidate-generation snapshot carried by a best_confirmed event.
    A confirmation pass spans several nodes; checking only the chosen node would still accept a
    winner computed using a reset competitor's stale seeds. Old events have no map and remain valid."""
    raw = d.get("generations", _MISSING)
    if raw is _MISSING:
        # Legacy best_confirmed (pre-generation-map). Modern producers ALWAYS stamp `generations`
        # (confirm_phase), so this branch is reached only by OLD persisted logs. Validate just the
        # CHOSEN winner: rejecting whenever ANY unrelated node was later aborted/tombstoned would
        # retroactively drop a legitimately-completed confirmation that the pre-batch fold accepted
        # (invariant 5b — an old log must fold as it did before). A winner that is itself
        # aborted/tombstoned is still correctly rejected.
        n = _node_for_event(st, d)
        return n is None or (not n.tombstoned and n.id not in st.aborted_nodes
                             and _generation_matches(n, d))
    if not isinstance(raw, dict):
        return False
    chosen = _coerce_node_id(d)
    seen: set[int] = set()
    for raw_nid, raw_generation in raw.items():
        nid = _coerce_node_id({"node_id": raw_nid})
        generation = _event_generation({"generation": raw_generation})
        if (nid is None or generation in (_MISSING, None)
                or nid not in st.nodes or nid in st.aborted_nodes
                or st.nodes[nid].tombstoned or st.nodes[nid].attempt != generation):
            return False
        seen.add(nid)
    if d.get("node_id") is not None and (chosen is None or chosen not in seen):
        return False
    # A candidate created while confirmation was running was absent from the snapshot and therefore
    # never compared. Do not mark confirmation complete until the snapshot exactly covers the current
    # candidate set (a reset is already caught by the per-entry generation checks above).
    active = {nid for nid, n in st.nodes.items()
              if nid not in st.aborted_nodes and not n.tombstoned}
    return seen == active


def _parent_generation_map_matches(st: RunState, d: dict) -> bool:
    """Atomically bind a derived node to the parent lifecycles used to build it.

    The engine captures this map before a potentially slow Researcher/Developer call. If a reset or
    abort lands before node_created, replay sees the changed parent first and rejects the stale child.
    Historical events may omit the map, but their declared parents must still exist and be active.
    """
    raw = d.get("parent_generations", _MISSING)
    parent_ids = d.get("parent_ids") or []
    if not isinstance(parent_ids, list):
        return False
    expected_parents: set[int] = set()
    for raw_parent in parent_ids:
        pid = _coerce_node_id({"node_id": raw_parent})
        if pid is None:
            return False
        expected_parents.add(pid)
    if raw is _MISSING:
        return all(pid in st.nodes and pid not in st.aborted_nodes
                   and not st.nodes[pid].tombstoned for pid in expected_parents)
    if not isinstance(raw, dict):
        return False
    seen: set[int] = set()
    for raw_pid, raw_generation in raw.items():
        pid = _coerce_node_id({"node_id": raw_pid})
        generation = _event_generation({"generation": raw_generation})
        parent = st.nodes.get(pid) if pid is not None else None
        if (pid is None or generation in (_MISSING, None) or parent is None
                or parent.tombstoned or parent.attempt != generation
                or pid in st.aborted_nodes):
            return False
        seen.add(pid)
    return seen == expected_parents


def _charge_terminal_cost(st: RunState, n: Node, d: dict, ctx: "_FoldCtx") -> None:
    """Charge eval compute once per lifecycle even when its terminal arrives after a reset. Generation
    guards protect state/selection, not the cumulative budget: discarding a metric must not refund the
    process time and make repeated resets a max_eval_seconds bypass."""
    generation = _event_generation(d, legacy_attempt=True)
    if generation is _MISSING:
        # Terminals have carried `attempt` since before lifecycle-wide `generation` stamps were
        # introduced. A truly unstamped terminal is therefore a legacy generation-0 record, not the
        # node's current generation (which could have advanced after a reset). Resolving it to the
        # current value would let one delayed duplicate charge the budget again under a fresh key.
        generation = 0
    # A late result may name an older lifecycle and its real compute still counts. An unknown/future
    # lifecycle is causally impossible, though, and must not be able to poison the budget.
    if generation is None or generation > n.attempt:
        return
    key = (n.id, generation)
    if key not in ctx.charged_terminal_generations:
        ctx.charged_terminal_generations.add(key)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))


def _on_node_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)                  # tolerate an event for an unknown/missing node
    if n is not None:
        if n.id in st.aborted_nodes:
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)  # stale metric ignored; real compute still spent
            return
        # Idempotent (C4): only a node's FIRST terminal event contributes its eval time, so
        # a duplicate node_evaluated/node_failed (corrupt log / double-fold) can't inflate
        # total_eval_seconds or make the budget order-dependent.
        # Invariant #2 "first terminal wins" applies to the WHOLE node, not just eval-seconds:
        # gate every field mutation on `first_terminal` so a CONFLICTING second terminal
        # (node_evaluated then node_failed, from a corrupt / double-appended log) can't flip the
        # node's metric/status/feasibility last-wins. A `node_reset` returns status to pending,
        # so a legitimate re-evaluation still applies (it IS the first terminal after the reset).
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            n.metric = _finite_metric(d.get("metric"))  # invalid/missing remains only in the raw log
            n.status = NodeStatus.evaluated
            n.terminal_event_seq = e.seq
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            n.stdout_tail = d.get("stdout_tail", "")
            n.eval_seconds = d.get("eval_seconds")
            n.extra_metrics = normalize_extra_metrics(d.get("extra_metrics"))
            n.violations = d.get("violations", []) or []
            n.feasible = not n.violations       # #5: constraint-violating -> infeasible
            # Intra-node sweep: per-trial results (audit/UI only; node.metric is already the
            # best trial, set by the engine). Coerce defensively per trial so one malformed
            # entry in a hand-edited/bring-your-own-script log can't crash the whole fold.
            trials = []
            for t_d in (d.get("trials", []) or []):
                try:
                    trials.append(Trial(**t_d))
                except Exception:
                    continue
            n.trials = trials
            _charge_terminal_cost(st, n, d, ctx)


_FAILURE_SPIKE_IGNORED_REASONS = {"aborted", "cancelled", "proxy_skipped", "superseded"}


def _counts_as_current_failure(st: RunState, n: Node) -> bool:
    return (n.status is NodeStatus.failed and not n.tombstoned and n.id not in st.aborted_nodes
            and str(n.error_reason or "").strip().lower() not in _FAILURE_SPIKE_IGNORED_REASONS)


def _add_current_failure(st: RunState, n: Node, event: Event) -> None:
    if not _counts_as_current_failure(st, n):
        return
    st.current_failure_count += 1
    level = st.current_failure_count // 3
    if level > st.failure_spike_level:
        st.failure_spike_seq = event.seq
    st.failure_spike_level = level


def _remove_current_failure(st: RunState, n: Node) -> None:
    if not _counts_as_current_failure(st, n):
        return
    st.current_failure_count = max(0, st.current_failure_count - 1)
    st.failure_spike_level = st.current_failure_count // 3


def _on_node_failed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    nid = _coerce_node_id(d)
    if nid is not None:
        _clear_build_marker(st, d, nid)
    if n is not None:
        if n.id in st.aborted_nodes and d.get("reason") != "aborted":
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)
            return
        # First-terminal-wins for the whole node (see node_evaluated above): a conflicting
        # second terminal from a corrupt log must not flip an already-evaluated node to failed.
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            n.status = NodeStatus.failed
            n.terminal_event_seq = e.seq
            n.error = d.get("error", "")
            n.error_reason = d.get("reason", "")
            # Crash-triage verdict, when the LLM triage ran (signal-delivery §1): fold it onto
            # the node so the failure-reflection hint / digest can hand it to the next proposal.
            # Additive + reader-defaulted: absent on old logs / rule-triaged nodes -> stays "".
            if d.get("triage_rationale"):
                n.triage_rationale = str(d.get("triage_rationale"))
            n.eval_seconds = d.get("eval_seconds")
            n.rerun_from = None
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            if d.get("failed_stage"):
                n.failed_stage = d.get("failed_stage")   # Phase 1: which pipeline stage broke
            _charge_terminal_cost(st, n, d, ctx)
            _add_current_failure(st, n, e)

def _on_node_repaired(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # In-node inline repair (hybrid crash repair): a NON-terminal event that replaces the
    # node's code with the LLM-repaired version BEFORE the eval that follows it. Idempotent
    # and replay-safe: only mutates while the node is still pending (the single terminal
    # event emitted at the end of the repair loop flips status off pending), so a duplicate
    # or post-terminal node_repaired (corrupt/double-fold) is a no-op — mirrors the
    # `first_terminal` guard above. The LLM/subprocess are never re-invoked; the final code
    # and metric/status are reconstructed purely from this event + the terminal event.
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d)
            and n.status is NodeStatus.pending):
        n.code = d.get("code", n.code)
        if d.get("files"):
            n.files = d["files"]
        if d.get("deleted"):
            n.deleted = d["deleted"]

def _requeue_partition_bound_results(st: RunState, *, fresh_node_ids: set[int]) -> None:
    """Make every surviving incumbent comparable on the newly-hidden partition.

    Host grading derives the ordinary search metric *and* every confirmation seed from the
    complement of ``_holdout_idx``.  Rotating that index while retaining those values mixes two
    different datasets in one ranking.  Re-open each evaluated incumbent as a fresh lifecycle so
    the normal eval path materializes its unchanged code on the new complement.  The generation
    bump is essential: it makes late epoch-N workers inert and gives the repeated physical eval its
    own cost-accounting key.  Nodes created/reset by the event that opened this epoch are already
    fresh and are excluded by ``fresh_node_ids``.
    """
    requeued: set[int] = set()
    for n in st.nodes.values():
        if (n.id in fresh_node_ids or n.id in st.aborted_nodes or n.tombstoned
                or n.status is not NodeStatus.evaluated):
            continue
        n.attempt += 1
        n.status = NodeStatus.pending
        n.terminal_event_seq = None
        n.metric = None
        n.error = ""
        n.error_reason = ""
        n.triage_rationale = ""
        n.stdout_tail = ""
        n.eval_seconds = None
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        n.holdout_metric = None
        n.generalization_gap = None
        n.verifier_score = None   # R1-c: a soundness score judged the OLD attempt's result — discard it
        n.stages = []
        n.failed_stage = None
        n.rerun_from = None
        n.rerun_stage = None
        requeued.add(n.id)

    if not requeued:
        return
    for nid in requeued:
        st.confirm_seed_results.pop(nid, None)
        st.proxy_scores.pop(nid, None)
    st.proxy_skipped = [nid for nid in st.proxy_skipped if nid not in requeued]
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in requeued]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in requeued]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in requeued]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in requeued]
    st.policy_scores = {}
    st.policy_chosen = None
    st.policy_reason = ""


def _rotate_search_epoch(st: RunState, *, requeue_partition_scores: bool,
                         fresh_node_ids: set[int] | None = None) -> None:
    """Advance one epoch and invalidate every value bound to the disclosed partition."""
    st.search_epoch += 1
    st.holdout_evaluated_ids.clear()
    st.holdout_epoch_aware = False   # the disclosure is consumed; the new epoch has none yet
    for candidate in st.nodes.values():
        if candidate.tombstoned or candidate.id in st.aborted_nodes:
            continue                         # post-hoc audit evidence is not part of the new pool
        if candidate.holdout_metric is not None:
            candidate.verifier_score = None  # it judged the disclosed holdout evidence being invalidated
        candidate.holdout_metric = None
        candidate.generalization_gap = None
    if requeue_partition_scores:
        _requeue_partition_bound_results(st, fresh_node_ids=fresh_node_ids or set())


def _invalidate_disclosed_holdout(
        st: RunState, *, fresh_node_ids: set[int] | None = None) -> bool:
    """Close a disclosed epoch once active search changes again."""
    if not st.holdout_evaluated_ids:
        return False
    # Requeue every incumbent (wiping its metric to force a re-eval on the newly-hidden complement)
    # ONLY when the disclosed holdout was epoch-aware. A legacy (pre-search-epoch) disclosure must
    # rotate WITHOUT the metric wipe, or replaying an old holdout_select log would drop incumbents the
    # pre-batch fold left intact and change the selected best (invariant 5b, F2).
    _rotate_search_epoch(
        st, requeue_partition_scores=st.holdout_epoch_aware, fresh_node_ids=fresh_node_ids)
    return True


def _on_node_tombstoned(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only delete (§6.3): mark the listed node ids (a node + its descendant subtree, computed
    # by the writer so the fold stays a pure, order-tolerant set op) as logically deleted. They REMAIN
    # in st.nodes — so parent links still resolve, node-id allocation never reuses the id, and the
    # delete is reversible/auditable — but the evaluated/feasible/breedable/pending helpers skip a
    # tombstoned node, so it is excluded from best-pick, breeding, confirmation, and re-eval.
    # Idempotent: setting the flag twice (duplicate/overlapping tombstone events) is a no-op. Ids
    # coerced defensively — a forged/unhashable id in a hand-edited log is skipped, not a fold crash.
    affected: set[int] = set()
    # `node_ids` MUST be a list. A forged/hand-edited event with a truthy SCALAR (e.g. {"node_ids": 42})
    # would make `42 or []` -> `42` and `for raw in 42` raise TypeError — and the fold loop has no
    # per-event try/except, so that one bad record bricks EVERY replay/resume/view of the run. Guard the
    # type like `_parent_generation_map_matches` already does for `parent_ids` (fold must stay total).
    raw_ids = d.get("node_ids")
    for raw in (raw_ids if isinstance(raw_ids, list) else []):
        nid = _coerce_node_id({"node_id": raw})
        n = st.nodes.get(nid) if nid is not None else None
        if n is not None and not n.tombstoned:
            _remove_current_failure(st, n)
            n.tombstoned = True
            n.rerun_from = None
            n.rerun_stage = None
            affected.add(n.id)
    if not affected:
        return
    # Remove only references/actions that name deleted lifecycles. A post-hoc delete of an already
    # finished run is an audit edit, not an implicit search reopen: the finish/report/finalization and
    # unaffected node evidence remain intact until an explicit resume creates the next epoch.
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in affected]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in affected]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in affected]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in affected]
    if st.champion in affected:
        st.champion = None
    if st.approval_subject in affected:
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
    if st.approved_node_id in affected:
        st.approved = False
        st.approved_node_id = None
    if st.pause_node_id in affected:
        st.paused = False
        st.pause_node_id = None
        st.pause_generation = None
    if st.building and st.building.get("node_id") in affected:
        st.building = None
    for _aff in affected:
        st.buildings.pop(_aff, None)   # a tombstoned subtree may hold several in-progress builds
    if st.finished:
        if ctx.best_confirmed in affected:
            ctx.best_confirmed = None
        return

    # During an active search the candidate-set mutation invalidates completion certificates. If a
    # holdout was already disclosed, rotate now and re-evaluate every surviving incumbent.
    st.confirmed_done = False
    ctx.best_confirmed = None
    st.approved = False
    st.awaiting_approval = False
    st.approval_subject = None
    st.approval_generation = None
    st.approved_node_id = None
    _invalidate_disclosed_holdout(st)

def _on_node_reset(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Re-run an EXISTING node in place (no new id). Discard its state FROM `from_stage` so it
    # becomes pending again; the engine then re-runs just that stage, appending fresh events for
    # the SAME id (which land as the first-terminal-after-reset). Replay-safe: the reset marks
    # where the old lifecycle is abandoned. `eval` = keep idea+code, just re-score (the normal
    # eval loop picks a pending-with-code node up — no marker). `implement`/`propose` = also drop
    # the code and flag `rerun_from` so the engine re-develops (re-proposes for `propose`).
    n = _node_for_event(st, d)
    if n is not None and not n.tombstoned and _control_generation_matches(n, d):
        _remove_current_failure(st, n)
        was_finished = st.finished
        holdout_was_disclosed = bool(st.holdout_evaluated_ids)
        old_generation = n.attempt
        stage = d.get("from_stage", "eval")
        # Bump the attempt generation (P0-1): the engine stamps this on the re-eval's terminal, and a
        # LATE terminal from the attempt this reset abandons carries the OLD generation and is dropped
        # by `_attempt_matches` — so an in-flight pre-reset eval can't land its metric on the new code.
        n.attempt += 1
        if st.pause_node_id == n.id and st.pause_generation == old_generation:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
        n.status = NodeStatus.pending
        n.terminal_event_seq = None
        n.metric = None
        n.error = ""
        n.error_reason = ""
        n.triage_rationale = ""   # the crash-triage verdict describes the NOW-abandoned lifecycle
        n.eval_seconds = None
        n.stdout_tail = ""
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        n.agent_report = None
        # The PER-SEED confirm memo must reset with the node too: the confirm phase memo-skips
        # every seed already in `confirm_seed_results`, so a stale entry would re-emit
        # node_confirmed from PRE-reset seed metrics for the post-reset code without running a
        # single seed. Pending force-confirm requests are lifecycle-scoped and are cancelled below;
        # completed fulfillment history stays for audit while its generation-aware twin prevents ABA.
        st.confirm_seed_results.pop(n.id, None)
        st.confirm_requests = [queued for queued in st.confirm_requests if queued != n.id]
        st.confirm_request_generations = [
            r for r in st.confirm_request_generations if r.get("node_id") != n.id]
        # Abort/proxy decisions belong to the lifecycle that was active when they were recorded.
        # Keeping them would immediately abort/skip every reset generation forever.
        st.aborted_nodes = [nid for nid in st.aborted_nodes if nid != n.id]
        st.proxy_scores.pop(n.id, None)
        st.proxy_skipped = [nid for nid in st.proxy_skipped if nid != n.id]
        st.ablate_requests = [nid for nid in st.ablate_requests if nid != n.id]
        st.ablate_request_generations = [
            r for r in st.ablate_request_generations if r.get("node_id") != n.id]
        if st.champion == n.id:
            st.champion = None
        ranked = st.hypothesis_ranking or {}
        if (ranked.get("node_id") == n.id
                and _event_generation(ranked) == old_generation):
            st.hypothesis_ranking = None
        n.failed_stage = None
        # Finish-time scores computed on the NOW-discarded code must not survive the reset, or a
        # holdout-gated best pick / generalization-gap audit keeps using a stale number the node
        # can no longer reproduce (holdout is append-only + skips already-scored ids, so it would
        # never be recomputed for this node). R1-c's verifier_score is exactly such a finish-time
        # score (a soundness judgment on the OLD attempt's result) — it must reset too, else the
        # tie-break would rank the new attempt by a score for a realization it no longer produces.
        n.holdout_metric = None
        n.verifier_score = None
        if n.id in st.holdout_evaluated_ids:
            st.holdout_evaluated_ids.remove(n.id)
        if stage in ("implement", "propose"):
            n.code = ""
            n.files = {}
            n.deleted = []
            n.stages = []                # a re-develop discards the old pipeline outcomes too
            n.rerun_from = stage
            n.rerun_stage = None
            # M1 (§21.18): drop the node's cached concept tags when they go STALE, so the next
            # concept-coverage cadence re-tags it fresh. Scope is tied to the TAGGER'S INPUTS: the snapshot
            # tagger reads only the IDEA (theme/rationale/params — `tools=None`, never the code), so tags
            # staleify only when the idea changes — i.e. `propose` (re-proposes a new idea), NOT `implement`
            # (re-develops CODE with the idea unchanged) nor `eval` (re-scores, idea+code unchanged). If the
            # tagger is later made agentic (reads code, `tools!=None`, §21.18 HT/B1), widen this to
            # `implement` too. No-op on old logs / untagged nodes.
            if stage == "propose":
                st.node_concepts.pop(n.id, None)
                st.node_concept_provenance.pop(n.id, None)
                st.node_concepts_at_vocab.pop(n.id, None)   # keep the B1 staleness map in sync
                # CODEX AGENT: the raw delta belongs to the Idea being abandoned. Clear it at the reset
                # boundary itself; otherwise a replay between reset and rebuild rematerializes stale
                # taxonomy for the pending node from a proposal that no longer exists.
                st.node_concept_deltas.pop(n.id, None)
                ctx.concept_mode_untrusted.discard(n.id)
                ctx.concept_input_capped.discard(n.id)
                ctx.concept_input_invalid.discard(n.id)
                # CODEX AGENT: generation stamps did not exist on early classifier events. Remember
                # the idea boundary inside this fold so those ambiguous receipts still fail closed,
                # while unstamped receipts after eval/implement-only attempt bumps remain readable.
                ctx.concept_subject_invalidated.add(n.id)
        else:
            # eval-type reset: pending-with-code, the eval loop re-scores it. `from_stage` names
            # the pipeline stage to RESTART from (Phase 2) — the eval re-runs from there, reusing
            # earlier stages' artifacts. Plain "eval" on a single-command node is a full re-score.
            n.rerun_from = None
            n.rerun_stage = stage
            # Preserve only stages strictly BEFORE the requested restart boundary. A new lifecycle
            # that fails early must not retain a later-stage success from the abandoned generation.
            for i, prior in enumerate(n.stages):
                if prior.get("name") == stage:
                    n.stages = n.stages[:i]
                    break
            if holdout_was_disclosed:
                # Stage reuse can retain a model trained on the old search complement. A disclosed
                # partition forces a full freshly-materialized eval in the next epoch; source code
                # survives, but no old stage artifact or workdir checkpoint may be reused.
                n.rerun_stage = None
                n.stages = []
        _clear_build_marker(st, d, n.id)
        # Reset itself clears `finished`, so a later resume cannot observe the old finished edge.
        # Invalidate the completed confirmation/approval epoch here, before clearing it.
        # Requeuing every OTHER incumbent (wiping its metric to force a re-eval on the newly-hidden
        # complement) is a NEW epoch-aware semantic. A legacy unstamped node_reset predates search
        # epochs; firing it there wipes surviving incumbents' metrics that the pre-batch fold left
        # intact — an invariant-5b divergence when replaying an old log. Gate the requeue-all on a
        # modern generation stamp. (A modern generation-0 reset that omits the stamp — allowed only at
        # attempt 0 — likewise skips it: a rare, benign fairness gap, never corruption.) The plain
        # finished-reopen epoch bump below is deliberately NOT gated: a reset is itself the reopen edge
        # and bumps the epoch regardless of stamp (it wipes no incumbent metric — requeue=False).
        reset_is_epoch_aware = _event_generation(d) is not _MISSING
        if holdout_was_disclosed and reset_is_epoch_aware:
            # The target is already a fresh pending generation. Every OTHER active incumbent must
            # also be re-evaluated on the newly-hidden complement; retaining its raw/confirm metric
            # would rank values measured on different partitions in one candidate pool.
            _rotate_search_epoch(
                st, requeue_partition_scores=True, fresh_node_ids={n.id})
        elif was_finished:
            # A reset is itself the actual reopen edge. With no disclosed partition there are no raw
            # scores to invalidate, but confirmation/approval still belong to the prior search epoch.
            _rotate_search_epoch(st, requeue_partition_scores=False)
        st.confirmed_done = False
        # `best_confirmed.generations` covers the whole candidate set. Resetting ANY competitor
        # invalidates the snapshot, even when the previously chosen winner itself was untouched.
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
        # A reset means there is work to do again, so it RE-OPENS a finished run — else the
        # loop would see the stale run_finished and exit before re-running/re-scoring the node.
        # (Mirrors EV_RESUME's finished-clear; a later run_finished sets it again. `paused` is
        # left alone — that's the operator's separate resume.)
        st.finished = False
        st.stop_reason = None
        st.stop_requested = None

def _on_stage_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Multi-stage eval pipeline (Phase 1): one stage of a node's declared pipeline finished.
    # Last-wins by stage name so a stage-scoped RE-RUN (Phase 2) replaces the prior outcome
    # rather than appending a duplicate.
    n = _node_for_event(st, d)
    if n is not None and n.id not in st.aborted_nodes and _generation_matches(n, d):
        rec = {"name": d.get("name"), "status": d.get("status"),
               "exit_code": d.get("exit_code"), "seconds": d.get("seconds")}
        for i, s in enumerate(n.stages):
            if s.get("name") == rec["name"]:
                # A "reused" marker means a re-eval SKIPPED this stage (an earlier attempt already
                # ran it) — it must NOT clobber that attempt's REAL completion record (its true
                # exit_code/seconds), else the node reads as if it trained in 0s. Keep the
                # informative record. Order-tolerant: a real record still replaces a prior reused.
                if rec["status"] == "reused" and s.get("status") not in (None, "reused"):
                    break
                n.stages[i] = rec
                break
        else:
            n.stages.append(rec)

def _on_confirm_eval(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    seed = _coerce_node_id({"node_id": d.get("seed")}) if "seed" in d else None
    keyed = nid is not None and seed is not None
    n = st.nodes.get(nid) if nid is not None else None
    legacy_attempt = "generation" not in d and "attempt" in d
    generation = _event_generation(d, legacy_attempt=True)
    # Fresh master briefly emitted `attempt`; preserve its historical behavior (a stale attempt is
    # fully dropped). Canonical `generation` events use the stricter lifecycle rule below: stale state
    # is inert, but already-spent compute still counts against the budget.
    if legacy_attempt and n is not None and (generation is None or generation != n.attempt):
        return
    # Old logs did not stamp confirm events: bind those to the extant lifecycle visible at that point.
    # Cost is trusted only for an evaluated lifecycle, an intervention-invalidated lifecycle, or an
    # older generation whose worker actually ran before reset. A forged current-generation event on a
    # still-pending node cannot reserve a seed's dedupe key and suppress the later real compute cost.
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    chargeable = (n is not None and isinstance(resolved_generation, int)
                  and resolved_generation <= n.attempt
                  and (resolved_generation < n.attempt
                       or n.status is NodeStatus.evaluated
                       or n.id in st.aborted_nodes or n.tombstoned))
    if keyed and chargeable and isinstance(resolved_generation, int):
        cost_key = (nid, resolved_generation, seed)
        if cost_key not in ctx.charged_confirm_seeds:
            ctx.charged_confirm_seeds.add(cost_key)
            _charge_eval_seconds(st, "confirm", d.get("eval_seconds"))
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return                    # stale metric/memo ignored; its real cost was charged above
    # Only a KEYED event (node_id+seed) can participate in the per-seed memo that makes the eval-cost
    # add idempotent; an un-keyed confirm_eval has no memo slot, so a duplicate/re-fold would
    # double-count total_eval_seconds (order/duplication-sensitive — the fold must not be). The sole
    # emitter always writes both keys, so this only guards a future/foreign/hand-edited un-keyed event.
    if keyed:                                                # per-seed resume memo (#0)
        st.confirm_seed_results.setdefault(nid, {})[seed] = _finite_metric(d.get("metric"))

def _on_node_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.status is NodeStatus.evaluated
            and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d, legacy_attempt=True)):
        # A confirmation certificate is one atomic evidence revision.  Validate every selection-bearing
        # field before touching the node: a torn/foreign row must neither create a partial certificate nor
        # erase the last valid certificate (or its verifier treatment).
        mean = _finite_metric(d.get("mean"))
        std = _finite_metric(d.get("std"))
        seeds = d.get("seeds")
        if (mean is None or std is None or std < 0.0
                or isinstance(seeds, bool) or not isinstance(seeds, int) or seeds <= 0):
            return
        # Confirmation changes the evidence revision judged by the verifier. Invalidate any earlier score;
        # a newly-emerged confirmed tie is re-scored as one complete group by the cadence producer.
        prior_evidence = verifier_evidence_digest(st.direction, n)
        n.confirmed_mean = mean
        n.confirmed_std = std
        n.confirmed_seeds = seeds
        if verifier_evidence_digest(st.direction, n) != prior_evidence:
            n.verifier_score = None

def _on_holdout_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # D1 holdout-gated promotion: the engine re-scored this val-leader's predictions on
    # the FINAL holdout partition the search never saw. Tolerant like node_evaluated:
    # an event for an unknown node (corrupt log) is skipped, and a null metric (missing
    # predictions) records nothing — such a node simply can't win the holdout pick.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    generation = _event_generation(d, legacy_attempt=True)
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return
    # A prior epoch's holdout was already disclosed; late scores from it cannot enter the newly
    # hidden partition's gate or metric pool. Missing epoch remains legacy-current.
    if d.get("search_epoch", st.search_epoch) != st.search_epoch:
        return
    if "search_epoch" in d:
        # A modern producer stamps `search_epoch` (holdout.py); a legacy holdout_evaluated does not.
        # Record that THIS disclosed holdout carries epoch semantics, so a later candidate change may
        # safely requeue incumbents onto the newly-hidden complement. A legacy (unstamped) disclosure
        # leaves this False, so the requeue-with-metric-wipe stays gated off (invariant-5b, F2).
        st.holdout_epoch_aware = True
    if nid is not None and nid not in st.holdout_evaluated_ids:
        st.holdout_evaluated_ids.append(nid)   # gate: attempted, even if metric is null
    metric = _finite_metric(d.get("metric"))
    if n is not None and metric is not None:
        prior_evidence = verifier_evidence_digest(st.direction, n)
        n.holdout_metric = metric
        if verifier_evidence_digest(st.direction, n) != prior_evidence:
            n.verifier_score = None

def _on_agent_validated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes
            and _generation_matches(n, d)):   # audit only; never affects selection
        n.agent_report = {
            "ok": d.get("ok"), "checks": d.get("checks", []),
            "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
            "shipped_ok": d.get("shipped_ok"),
        }

def _on_data_profiled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.data_profile = d.get("columns")

def _on_data_provenance(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.data_provenance = d   # D4: pinned dataset/asset content hashes

def _on_host_grading(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.host_grading = d      # out-of-process host-side grading active (audit; no labels)

def _on_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-3: setup completed (task+data preflight, incl. the leakage hard-stop). Folded so resume can
    # tell "setup done" from "crashed mid-setup right after run_started" — the latter must re-run the
    # rest of preflight (leakage!) rather than skip it forever. Idempotent (a re-run re-appends it).
    st.setup_done = True
    # P0-3 manifest: bind the completion to the material it verified (config/workspace/data digest).
    # Additive: absent on old logs -> "" -> resume falls back to the boolean (unchanged behavior).
    if d.get("manifest"):
        st.setup_manifest = str(d.get("manifest"))

def _on_run_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # arch-review §5 P2: a SUCCESSFUL run-level `run_setup` (dep install) is folded (keyed by its
    # command) so a resume skips it instead of re-installing every time — crash-safe exactly-once. A
    # failed/timed-out setup is NOT recorded (the command must actually re-run). Old logs whose
    # run_setup_finished carried no `command` just don't populate the set (setup runs as before).
    if d.get("exit_code") == 0 and not d.get("timed_out") and d.get("command"):
        st.run_setup_done.add(run_setup_key(d.get("command")))

def _on_data_leakage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.leakage = d

def _on_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if "after_seq" in d:
        raw_after = d.get("after_seq")
        if isinstance(raw_after, bool):
            return
        try:
            after_seq = int(raw_after)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return
    if st.approved:
        return                         # a grant that won the race cannot be re-opened by a stale request
    subject = _coerce_node_id(d)
    node = st.nodes.get(subject) if subject is not None else None
    if node is not None and (node.id in st.aborted_nodes or node.tombstoned):
        return
    generation = _event_generation(d)
    if (subject is not None and generation is not _MISSING
            and (node is None or not _generation_matches(node, d))):
        return
    same_pending = (st.awaiting_approval and st.approval_subject == subject
                    and st.approval_generation == (node.attempt if node is not None else None))
    st.awaiting_approval = True
    # P0-2: record WHICH node the request is for (the engine emits the current best) as audit context,
    # surfaced in the projection so the UI can show what is awaiting approval. This is NOT the grant
    # gate — `_on_approval_granted` binds to node existence, not to this subject (see there).
    st.approval_subject = subject
    st.approval_generation = node.attempt if node is not None else None
    if not same_pending:
        st.approval_request_seq = e.seq

def _on_approval_granted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2 approval gate: honor a grant that names a REAL node in the run — the current best OR an
    # operator-chosen node (`approve --node-id N` / the boss `approve` action both ratify a specific
    # node). A grant for a node that doesn't exist — a forged/typo'd `approval_granted(node_id=999)`, or
    # an unhashable/bool/non-numeric id — is ignored, so it can't globally flip `approved`; the run stays
    # awaiting the real approval. Binding to node EXISTENCE (deliberately NOT to the pending
    # `approval_subject`) closes the forged-id hole while still allowing a legitimate non-best `--node-id`
    # grant. The id is coerced/guarded by `_coerce_node_id` BEFORE the membership test so a forged
    # unhashable id can't raise inside the `in` and brick the fold. Back-compat: a bare grant with no
    # node_id (old logs / a direct grant) is accepted, so legacy HITL runs fold identically.
    if d.get("node_id") is not None:               # a TARGETED grant must name a real, coercible node
        subj = _coerce_node_id(d)
        if subj is None or subj not in st.nodes:
            return                                 # forged / unhashable / non-existent -> ignore
        node = st.nodes[subj]
        if node.id in st.aborted_nodes or node.tombstoned:
            return
        generation = _event_generation(d)
        if generation is not _MISSING and not _generation_matches(node, d):
            return
        st.approved_node_id = subj
    else:
        # Bare grants are legacy. Modern first-party producers always name + generation-stamp a node;
        # accepting this shape is solely persisted-log compatibility.
        st.approved_node_id = st.approval_subject
    st.awaiting_approval = False
    st.approved = True
    st.approval_subject = None
    st.approval_generation = None

def _on_spec_proposed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # The request is the human-review boundary. Once it exists (and especially after ratification),
    # a late agent event must not swap in content the operator never reviewed under the same card.
    if st.spec_approval_requested or st.spec_confirmed:
        return
    st.proposed_spec = d

def _on_spec_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A request without a proposal can never be ratified. Treat it as malformed instead of exposing
    # an actionable phase that every first-party approval producer must reject.
    if st.proposed_spec is None or st.spec_confirmed:
        return
    if not st.spec_approval_requested:
        st.spec_approval_request_seq = e.seq
    st.spec_approval_requested = True

def _on_spec_approved(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2: ratify only a spec that was actually PROPOSED. A premature/forged `spec_approved` (no
    # preceding `spec_proposed`) would set `spec_confirmed=True` while `proposed_spec` is None,
    # skipping onboarding entirely. The real flow always folds `spec_proposed` first (the engine
    # gates the emit on it), so this only rejects an out-of-order ratification; old logs are
    # unaffected (they always carry the proposal).
    if st.proposed_spec is not None:
        st.spec_confirmed = True

def _on_spec_drift(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    generation = _event_generation(d)
    if generation is not _MISSING:
        n = _node_for_event(st, d)
        if n is None or n.id in st.aborted_nodes or not _generation_matches(n, d):
            return
    st.drifts.append(d)                         # audit only; metric already discarded

def _on_workspace_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.workspace_changed = True                 # resume saw the source repo/data change


def _on_env_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.env_changed = True                       # resume saw the Python/lib environment drift (F18)

def _on_diversity_archive(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.archive = d

def _on_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.coverage_snapshots.append(d)   # audit-only breadth curve; the at_node gate dedups on resume

_MAX_LLM_COUNTER = (1 << 63) - 1
_MAX_LLM_COST = 1.7976931348623157e308


def _llm_counter(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= _MAX_LLM_COUNTER else 0


def _llm_cost_value(value) -> float:
    if not is_usable_metric(value):
        return 0.0
    out = float(value)
    return out if out >= 0.0 else 0.0


def _clean_llm_totals(d: dict | None) -> dict:
    try:
        raw = dict(d) if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 - a corrupt event must not poison every replay
        raw = {}
    out = dict(raw)
    out.update({
        "cost": _llm_cost_value(raw.get("cost")),
        "calls": _llm_counter(raw.get("calls")),
        "prompt_tokens": _llm_counter(raw.get("prompt_tokens")),
        "completion_tokens": _llm_counter(raw.get("completion_tokens")),
        "total_tokens": _llm_counter(raw.get("total_tokens")),
    })
    return out


def _on_run_concepts(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """PART V (B): set the RUN's BASE concept set (last-write-wins). Nodes may then author only deltas vs
    this base; the fold post-pass materializes their node_concepts. Additive; malformed input -> ignored."""
    concepts = d.get("concepts")
    if isinstance(concepts, list):
        base, overflow, invalid = bounded_raw_concept_values(concepts)
        # CODEX AGENT: keep the folded/FoldCursor base bounded; the append-only event remains the raw
        # audit source. Last valid run_concepts wins for both membership and its integrity receipt.
        st.run_base_concepts = list(dict.fromkeys(base))
        ctx.run_base_capped = overflow
        ctx.run_base_invalid = invalid
        ctx.run_base_seen = True


def _materialize_concept_deltas(
    st: RunState,
    *,
    untrusted_modes: set[int] | None = None,
    capped_inputs: set[int] | None = None,
    invalid_inputs: set[int] | None = None,
    base_capped: bool = False,
    base_invalid: bool = False,
    run_base_seen: bool = True,
) -> None:
    """Iteratively materialize delta memberships with typed partial/unavailable receipts.

    The post-pass sees the complete folded DAG, which makes it event-order tolerant and safe for very
    deep lineages. Identity failures omit only malformed operands (``partial``); missing/unknown parents,
    unsupported modes, and dependency cycles make the result ``unavailable`` and propagate unchanged.
    """
    # CODEX AGENT: receipts are derived from scratch on every full fold and FoldCursor snapshot. A
    # repaired suffix therefore clears stale failures instead of carrying snapshot-finalization state.
    seed_reasons: dict[int, set[ConceptMaterializationReason]] = {
        nid: {CONCEPT_MODE_UNSUPPORTED_REASON}
        for nid in sorted(untrusted_modes or ()) if nid in st.nodes
    }
    for nid in sorted(capped_inputs or ()):
        if nid in st.nodes:
            seed_reasons.setdefault(nid, set()).add(CONCEPTS_PER_NODE_CAP_REASON)
    for nid in sorted(invalid_inputs or ()):
        if nid in st.nodes:
            seed_reasons.setdefault(nid, set()).add(CONCEPT_INVALID_ID_REASON)
    renames = normalized_concept_renames(getattr(st, "concept_consolidation", None))
    base, base_reasons = resolve_concept_set_reasons(st.run_base_concepts, renames)
    if base_capped:
        base_reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
    if base_invalid:
        base_reasons.add(CONCEPT_INVALID_ID_REASON)
    if renames.endpoint_problem:
        # Invalid unused endpoints do not erase resolvable ids, but the projection is only partial.
        base_reasons.add(CONCEPT_INVALID_ID_REASON)
    active = {nid for nid in st.node_concept_deltas
              if st.node_concept_provenance.get(nid) == NODE_CONCEPT_PROVENANCE_AUTHORED}
    any_component_needs_run_base = any(
        node is not None and not (getattr(node, "parent_ids", None) or [])
        for nid in active if (node := st.nodes.get(nid)) is not None
    )
    public_base_reasons = set(base_reasons)
    if any_component_needs_run_base and not run_base_seen:
        # CODEX AGENT: an absent EV_RUN_CONCEPTS is not an exact empty base. Order-tolerant logs may append
        # the base after their nodes, so a live prefix must fail closed until that inheritance source exists;
        # an explicit ``run_concepts: []`` sets ``run_base_seen`` and remains a valid known-empty base.
        base_reasons.add(CONCEPT_DELTA_MISSING_RUN_BASE_REASON)

        # The node receipt remains on historical tombstoned/aborted roots, but the public run-base receipt
        # must poison today's ConceptFrame only when a CURRENT delta component actually reaches such a root.
        # Walk current nodes' authored-delta ancestors iteratively so an inactive root with a live descendant
        # still fails closed, while a disconnected deleted component cannot corrupt an honestly exact frame.
        pending = [nid for nid in active
                   if (node := st.nodes.get(nid)) is not None
                   and nid not in st.aborted_nodes and not node.tombstoned]
        visited: set[int] = set()
        current_needs_run_base = False
        while pending and not current_needs_run_base:
            nid = pending.pop()
            if nid in visited:
                continue
            visited.add(nid)
            node = st.nodes.get(nid)
            if node is None:
                continue
            parents = getattr(node, "parent_ids", None) or []
            if not parents:
                current_needs_run_base = True
                break
            pending.extend(parent_id for parent_id in parents
                           if parent_id in active and parent_id not in visited)
        if current_needs_run_base:
            public_base_reasons.add(CONCEPT_DELTA_MISSING_RUN_BASE_REASON)
    st.run_base_concept_receipt = concept_materialization_receipt(public_base_reasons)

    dependencies: dict[int, set[int]] = {}
    children: dict[int, set[int]] = {nid: set() for nid in active}
    for nid in active:
        node = st.nodes.get(nid)
        parents = (getattr(node, "parent_ids", None) or []) if node is not None else []
        dependencies[nid] = {parent_id for parent_id in parents if parent_id in active}
        for parent_id in dependencies[nid]:
            children[parent_id].add(nid)

    ready = [nid for nid, parents in dependencies.items() if not parents]
    heapq.heapify(ready)
    effective: dict[int, set[str]] = {}
    reasons_by_node: dict[int, set[ConceptMaterializationReason]] = {
        nid: set(reasons) for nid, reasons in seed_reasons.items()
    }
    while ready:
        nid = heapq.heappop(ready)
        node = st.nodes.get(nid)
        parents = (getattr(node, "parent_ids", None) or []) if node is not None else []
        materialized = BoundedConceptAccumulator()
        reasons = set(reasons_by_node.get(nid, ()))
        delta = st.node_concept_deltas.get(nid)
        if not isinstance(delta, dict):
            reasons.add(CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON)
            removed: set[str] = set()
            added: set[str] = set()
        else:
            removed, removed_problems = resolve_concept_set_reasons(delta.get("removed"), renames)
            added, added_problems = resolve_concept_set_reasons(delta.get("added"), renames)
            reasons.update(removed_problems)
            reasons.update(added_problems)
        seed_receipt = concept_materialization_receipt(reasons)
        unavailable = bool(
            seed_receipt is not None and seed_receipt["status"] == "unavailable")
        if node is None:
            reasons.add(CONCEPT_DELTA_MISSING_PARENT_REASON)
            unavailable = True
        elif parents:
            for parent_id in parents:
                if parent_id in active:
                    parent_reasons = reasons_by_node.get(parent_id, set())
                    reasons.update(parent_reasons)
                    parent_receipt = concept_materialization_receipt(parent_reasons)
                    if parent_receipt is not None and parent_receipt["status"] == "unavailable":
                        unavailable = True
                    else:
                        materialized.update(
                            value for value in effective.get(parent_id, set()) if value not in removed)
                    continue
                if parent_id not in st.nodes:
                    reasons.add(CONCEPT_DELTA_MISSING_PARENT_REASON)
                    unavailable = True
                    continue
                parent_seed = reasons_by_node.get(parent_id, set())
                if parent_seed:
                    reasons.update(parent_seed)
                    parent_seed_receipt = concept_materialization_receipt(parent_seed)
                    if (parent_seed_receipt is not None
                            and parent_seed_receipt["status"] == "unavailable"):
                        unavailable = True
                        continue
                if parent_id not in st.node_concepts:
                    reasons.add(CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON)
                    unavailable = True
                    continue
                parent_provenance = st.node_concept_provenance.get(parent_id)
                # CODEX AGENT: an explicit full-set producer may be low-trust display taxonomy and still
                # define inheritance (offline heuristic), but an unknown/future producer or missing
                # provenance is not an exact set. Classifier/operator/authored-full remain authoritative.
                if parent_provenance not in {
                    NODE_CONCEPT_PROVENANCE_AUTHORED,
                    NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                    NODE_CONCEPT_PROVENANCE_OPERATOR,
                    NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                }:
                    reasons.add(CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON)
                    unavailable = True
                    continue
                parent_concepts, parent_problems = resolve_concept_set_reasons(
                    st.node_concepts[parent_id], renames)
                materialized.update(value for value in parent_concepts if value not in removed)
                reasons.update(parent_problems)
        else:
            materialized.update(value for value in base if value not in removed)
            reasons.update(base_reasons)
            base_receipt = concept_materialization_receipt(base_reasons)
            unavailable = bool(
                base_receipt is not None and base_receipt["status"] == "unavailable")
        # Remove is applied while streaming every inherited source; add is applied last and therefore
        # wins for tolerant legacy rows that ambiguously contain the same canonical id in both lists.
        materialized.update(added)
        receipt = concept_materialization_receipt(reasons)
        if receipt is not None and receipt["status"] == "unavailable":
            unavailable = True
        if materialized.overflow:
            reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
        effective[nid] = set() if unavailable else set(materialized.values)
        if reasons:
            reasons_by_node[nid] = reasons
        for child_id in sorted(children[nid]):
            dependencies[child_id].discard(nid)
            if not dependencies[child_id]:
                heapq.heappush(ready, child_id)

    # Kahn leaves cycle members and every active descendant of their undefined output unresolved. Seed
    # the cycle cause, also inspect direct non-cycle parents, then propagate the bounded closed reason set
    # through the unresolved subgraph. Fixing one cycle must not hide a second independent unavailable cause.
    unresolved = active - effective.keys()
    pending: list[int] = []
    queued: set[int] = set()
    for nid in sorted(unresolved):
        effective[nid] = set()
        reasons = reasons_by_node.setdefault(nid, set())
        reasons.add(CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON)
        raw_delta = st.node_concept_deltas.get(nid)
        if isinstance(raw_delta, dict):
            _removed, removed_problems = resolve_concept_set_reasons(
                raw_delta.get("removed"), renames)
            _added, added_problems = resolve_concept_set_reasons(
                raw_delta.get("added"), renames)
            reasons.update(removed_problems)
            reasons.update(added_problems)
        else:
            reasons.add(CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON)
        node = st.nodes.get(nid)
        unresolved_parents = (getattr(node, "parent_ids", None) or []) if node is not None else []
        for parent_id in unresolved_parents:
            if parent_id in active:
                if parent_id not in unresolved:
                    reasons.update(reasons_by_node.get(parent_id, ()))
                continue
            if parent_id not in st.nodes:
                reasons.add(CONCEPT_DELTA_MISSING_PARENT_REASON)
                continue
            parent_seed = reasons_by_node.get(parent_id, set())
            if parent_seed:
                reasons.update(parent_seed)
                parent_seed_receipt = concept_materialization_receipt(parent_seed)
                if (parent_seed_receipt is not None
                        and parent_seed_receipt["status"] == "unavailable"):
                    continue
            if (parent_id not in st.node_concepts
                    or st.node_concept_provenance.get(parent_id) not in {
                        NODE_CONCEPT_PROVENANCE_AUTHORED,
                        NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                        NODE_CONCEPT_PROVENANCE_OPERATOR,
                        NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                    }):
                reasons.add(CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON)
                continue
            _concepts, parent_problems = resolve_concept_set_reasons(
                st.node_concepts[parent_id], renames)
            reasons.update(parent_problems)
        heapq.heappush(pending, nid)
        queued.add(nid)
    while pending:
        parent_id = heapq.heappop(pending)
        queued.discard(parent_id)
        for child_id in sorted(children.get(parent_id, ()) & unresolved):
            child_reasons = reasons_by_node.setdefault(child_id, set())
            before = len(child_reasons)
            child_reasons.update(reasons_by_node.get(parent_id, ()))
            if len(child_reasons) != before and child_id not in queued:
                heapq.heappush(pending, child_id)
                queued.add(child_id)

    st.node_concept_materialization_receipts = {}
    for nid, reasons in sorted(reasons_by_node.items()):
        receipt = concept_materialization_receipt(reasons)
        if receipt is not None:
            st.node_concept_materialization_receipts[nid] = receipt
    for nid in active:
        st.node_concepts[nid] = sorted(effective.get(nid, set()))


def _on_concept_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV Phase 2a: the fold only retains the coverage / uncovered-region curve and never selects
    # from it; the live proposal path may later consume the record as a steering cue. at_node dedups resume.
    st.concept_coverage_snapshots.append(d)

def _on_node_concepts(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV D5 Phase 2c: the LLM tagger's RAW tags for one node, recorded once so later cadences reuse
    # them. Node/lifecycle-scoped; LAST valid write wins (a re-tag after graph growth may refine a node's
    # tags). It feeds read models and the opt-in graded-novelty admission precheck, so provenance and
    # generation matching below are a trust boundary rather than audit-only decoration.
    nid = _coerce_node_id(d, "node_id")
    if nid is None:
        return
    node = st.nodes.get(nid)
    generation = _event_generation(d)
    # Modern cadence events are lifecycle-stamped. A legacy unstamped event remains safe across an
    # eval/implement retry because the tagger's subject (the Idea) did not change; after a propose reset
    # or malformed subject-changing replacement it is indistinguishable from a late old-Idea result.
    # Unknown nodes and explicit stale/invalid generations remain fail-closed.
    if node is None:
        return
    incoming_provenance = node_concept_event_provenance(d)
    current_provenance = st.node_concept_provenance.get(nid)
    # Phase 2b: an OPERATOR edit is authoritative and must not be clobbered by the classifier cadence.
    # Checked BEFORE the generation gate so the classifier yields regardless of arrival order (invariant 5):
    # {classifier, operator} folds to the operator's tags either way. A PROPOSE reset (the idea changed)
    # clears node_concepts/provenance so the classifier re-tags the fresh node — the intended way to drop
    # an operator override; an implement/eval re-run keeps the same idea, so the operator tags rightly stand.
    if current_provenance == NODE_CONCEPT_PROVENANCE_OPERATOR:
        return
    # A coarse/future producer may enrich an authored/empty display, but must never overwrite or
    # downgrade a reviewed classifier receipt. This makes classifier/offline replay order-safe:
    # once independent evidence exists, a later local fallback cannot replace its tags or provenance.
    if (current_provenance == NODE_CONCEPT_PROVENANCE_CLASSIFIER
            and incoming_provenance != NODE_CONCEPT_PROVENANCE_CLASSIFIER):
        return
    if generation is _MISSING:
        # CODEX AGENT: lifecycle generation != concept-subject generation. Preserve legacy replay
        # after same-Idea retries, but never guess once this node crossed an observed Idea boundary.
        if nid in ctx.concept_subject_invalidated:
            return
    elif generation is None or generation != node.attempt:
        return
    concepts = d.get("concepts")
    bounded, overflow, invalid = bounded_raw_concept_values(concepts)
    st.node_concepts[nid] = bounded
    st.node_concept_provenance[nid] = incoming_provenance
    ctx.concept_input_capped.discard(nid)
    ctx.concept_input_invalid.discard(nid)
    if overflow:
        ctx.concept_input_capped.add(nid)
    if invalid:
        ctx.concept_input_invalid.add(nid)
    if incoming_provenance != NODE_CONCEPT_PROVENANCE_UNTRUSTED:
        ctx.concept_mode_untrusted.discard(nid)
    # B1 (§21.18): remember the vocabulary size at tag time so the cadence can spot tags made against an
    # out-of-date (smaller) vocabulary and refresh them. Absent on pre-B1 events -> no receipt (oldest).
    av = d.get("at_vocab")
    # CODEX AGENT: only classifier vocabulary receipts may delay the classifier refresh cadence.
    # An offline/future producer's integer is display metadata, not proof of semantic classification.
    if (incoming_provenance == NODE_CONCEPT_PROVENANCE_CLASSIFIER
            and isinstance(av, int) and not isinstance(av, bool) and av >= 0):
        st.node_concepts_at_vocab[nid] = av
    else:
        st.node_concepts_at_vocab.pop(nid, None)


def _on_concept_tag_edited(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """PART V Phase 2b: an OPERATOR replaces ONE node's concept tags. Authoritative for the run's read
    models — stamped OPERATOR provenance so `_on_node_concepts` (the classifier cadence) yields to it
    regardless of arrival order (invariant 5). The command layer generation-fences the intent (matches
    node.attempt) before it is appended, so the fold trusts a recorded edit and only re-checks the node
    exists. Last operator edit in log order wins (like the classifier cadence). A `node_generation`, when
    present, is honored the same way as the classifier's generation gate so a stale edit from a since-reset
    node is dropped. The override survives an implement/eval re-run of the SAME idea (see the re-emit guard
    in _on_node_created); only a PROPOSE reset (idea change) clears it. Concepts are a bounded list of
    strings; NOT independent evidence (provenance sidecar)."""
    nid = _coerce_node_id(d, "node_id")
    if nid is None:
        return
    node = st.nodes.get(nid)
    if node is None:
        return
    # A recorded operator edit carries the node generation it was formed against (`node_generation`, the
    # same field the comment lifecycle uses). If present it must match the live attempt — a reset (which
    # clears node_concepts/provenance) invalidates a pre-reset edit; absent (older intent) stays permissive.
    raw_generation = d.get("node_generation")
    if raw_generation is not None:
        generation = _coerce_node_id({"node_id": raw_generation})
        if generation is None or generation != node.attempt:
            return
    concepts = d.get("concepts")
    bounded, overflow, invalid = bounded_raw_concept_values(concepts)
    st.node_concepts[nid] = bounded
    st.node_concept_provenance[nid] = NODE_CONCEPT_PROVENANCE_OPERATOR
    ctx.concept_mode_untrusted.discard(nid)
    ctx.concept_input_capped.discard(nid)
    ctx.concept_input_invalid.discard(nid)
    if overflow:
        ctx.concept_input_capped.add(nid)
    if invalid:
        ctx.concept_input_invalid.add(nid)
    # Operator tags are not vocabulary-versioned; clear any classifier staleness receipt for this node.
    st.node_concepts_at_vocab.pop(nid, None)


def _on_hypothesis_concepts(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV D4 (§21.18 HT): the LLM tagger's concept ids for one hypothesis, recorded once so taxonomy
    # dedup reuses them. Hypothesis-scoped (str id); LAST write wins (a merge may re-derive the survivor's
    # tags). Audit-only — NEVER selection. Order-tolerant + idempotent + malformed-safe.
    hid = d.get("hyp_id")
    if not hid:
        return
    concepts = d.get("concepts")
    st.hypothesis_concepts[str(hid)] = [str(c) for c in concepts] if isinstance(concepts, list) else []
    av = d.get("at_vocab")   # B1-ext: staleness reference (absent on pre-B1 events -> 0/oldest)
    if isinstance(av, int) and not isinstance(av, bool) and av >= 0:   # bool is an int subclass — reject
        st.hypothesis_concepts_at_vocab[str(hid)] = av
    else:
        # CODEX AGENT: concepts and their vocabulary receipt are one LWW value. An older receipt would
        # make newly-derived tags look fresh and incorrectly suppress their next retag cadence.
        st.hypothesis_concepts_at_vocab.pop(str(hid), None)

def _on_concept_consolidation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV D5 B3 (§21.18): ACCUMULATE the consolidation rename map so decisions stay fixed across
    # cadences (stable vocabulary). Audit-only. Idempotent + malformed-safe. ORDER-TOLERANT (invariant 5):
    # a CONFLICTING re-map of the same raw id (raw->a in one event, raw->b in another) resolves to a
    # DETERMINISTIC winner — the lexicographically smallest canonical — never last-write, so
    # fold(perm(events)) is byte-identical. The B3 producer fixes each decision once and never re-maps an
    # existing raw id, so a conflict only arises in an adversarial / spliced log; this just hardens it.
    rename = d.get("rename")
    if isinstance(rename, dict):
        for raw, canon in rename.items():
            if raw and canon:
                raw, canon = str(raw), str(canon)
                cur = st.concept_consolidation.get(raw)
                st.concept_consolidation[raw] = canon if cur is None else min(cur, canon)


_EDGE_PROV_RANK = {"asserted": 2, "evidenced": 1}


def _on_concept_edge(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV concept-edge substrate: fold typed edges (src, rel, dst) COMMUTATIVELY — max-confidence-wins
    # keyed on the triple, ties by provenance-rank then lexicographic provenance — so replaying the same
    # edge's events in ANY order yields the same map (invariant 5 order-tolerance), unlike last-write.
    # Audit-only; NEVER touches selection. Accepts a batch (`edges: [...]`) or one inline edge; a
    # malformed row is skipped, never crashes the fold.
    raw = d.get("edges")
    rows = raw if isinstance(raw, list) else ([d] if all(k in d for k in ("src", "rel", "dst")) else [])
    for ed in rows:
        if not isinstance(ed, dict):
            continue
        src, rel, dst = (str(ed.get("src") or "").strip(), str(ed.get("rel") or "").strip(),
                         str(ed.get("dst") or "").strip())
        if not (src and rel and dst):
            continue
        if rel == "co_occurs":
            # CODEX AGENT: this relation is a cache of current node membership, not an immutable
            # assertion. The old max-wins fold cannot express count decreases or deletion, so retaining
            # legacy rows creates permanent ghost edges. ConceptFrame derives it from the exact folded
            # membership snapshot; omit it here so large legacy caches cannot consume live edge budgets.
            continue
        conf = ed.get("confidence")
        # REVIEW(2026-07-16): the tuple order below can only rank a REAL finite/±inf float. Two agent-
        # supplied values must be neutralized to keep the fold commutative (invariant 5 order-tolerance):
        #   * NaN — every `>` comparison against a NaN tuple-head is False, so whichever edge arrived
        #     FIRST would stick forever ([nan, 5.0] keeps nan while [5.0, nan] keeps 5.0). NaN is reachable
        #     because stdlib json round-trips `NaN` literals by default (dumps allow_nan / loads parses).
        #   * bool — isinstance(True, int) is True, so a stray `confidence: true` would coerce to 1.0 and
        #     could WIN over a legitimate edge; treat it as 0.0 (lowest) so a mis-typed flag never ranks.
        # Both map to 0.0, keeping the order total and the accumulate order-independent. (`x != x` is the
        # portable NaN test — no math import.)
        conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
        if conf != conf:
            conf = 0.0
        # CODEX AGENT: -0.0 and 0.0 tie numerically but serialize differently. Canonicalize the sign
        # before the commutative max so replay order cannot leak into RunState / ConceptFrame bytes.
        if conf == 0.0:
            conf = 0.0
        prov = str(ed.get("provenance") or "")
        key = "\t".join((src, rel, dst))
        cur = st.concept_edges.get(key)
        # A total order on (confidence, provenance-rank, provenance) makes the winner a pure function of
        # the two candidates, independent of arrival order — a commutative accumulate.
        if cur is None or ((conf, _EDGE_PROV_RANK.get(prov, 0), prov)
                           > (cur["confidence"], _EDGE_PROV_RANK.get(cur["provenance"], 0),
                              cur["provenance"])):
            st.concept_edges[key] = {"src": src, "rel": rel, "dst": dst,
                                     "provenance": prov, "confidence": conf}


def _on_llm_cost(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if not ctx.llm_usage_seen:
        # Compatibility base: latest legacy summary before the new ledger. Once a usage delta is
        # present, later summaries are derived snapshots and may not overwrite durable totals.
        st.llm_cost = _clean_llm_totals(d)


def _on_llm_usage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    usage_id = d.get("usage_id")
    if isinstance(usage_id, str) and usage_id:
        if usage_id in ctx.llm_usage_ids:
            ctx.llm_usage_seen = True
            return
        ctx.llm_usage_ids.add(usage_id)
    base = _clean_llm_totals(st.llm_cost)
    delta = _clean_llm_totals(d)
    base["cost"] = min(_MAX_LLM_COST, float(base["cost"]) + float(delta["cost"]))
    for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
        base[key] = min(_MAX_LLM_COUNTER, int(base[key]) + int(delta[key]))
    st.llm_cost = base
    ctx.llm_usage_seen = True

def _on_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    pid = _coerce_node_id(d, "parent_id")
    n = st.nodes.get(pid) if pid is not None else None
    generation = _event_generation(d)
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    valid = (generation is _MISSING
             or (n is not None and isinstance(resolved_generation, int)
                 and resolved_generation <= n.attempt))
    if pid is None or not valid or not isinstance(resolved_generation, int):
        return
    record = dict(d)
    record["parent_id"] = pid
    record.setdefault("generation", resolved_generation)
    st.ablations.append(record)   # historical audit; consumers/gates key it by lifecycle generation
    # Account the ablation probes' eval wall-clock against the cumulative budget (arch-review §4 P1-2:
    # ablation was wholly outside accounting, so a run could spend well past max_eval_seconds on
    # probes). Additive + reader-defaulted: old ablate events carry no eval_seconds -> +0.0.
    ablation_id = d.get("ablation_id")
    # New emitters identify one physical probe operation, so a duplicated append is idempotent while
    # two legitimate cadence runs on the same parent/generation both count. Legacy events had no id and
    # are therefore charged individually; collapsing them by parent would undercount real repeated work.
    if not isinstance(ablation_id, str) or not ablation_id:
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))
    elif ablation_id not in ctx.charged_ablation_ids:
        ctx.charged_ablation_ids.add(ablation_id)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))

def _on_policy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _scores = {}
    _raw = d.get("scores")
    # A non-dict `scores` (a list/str/number from a corrupt or hand-edited log) has no `.items()`
    # and would raise an uncaught AttributeError that bricks the ENTIRE fold — the same corrupt-log
    # class the per-key try/except below already guards. Skip a non-dict container the same way.
    for k, v in (_raw.items() if isinstance(_raw, dict) else ()):
        try:
            _scores[int(k)] = v                 # a non-integer key (corrupt log) is skipped
        except (TypeError, ValueError):
            continue
    st.policy_scores = _scores
    st.policy_chosen = d.get("chosen")
    st.policy_reason = d.get("reason") or ""

def _on_strategy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 Strategist (audit-only): the engine recorded the chosen Strategy. Replay rebuilds
    # active_strategy WITHOUT re-calling the LLM (the decision is config, not selection).
    st.active_strategy = d.get("strategy")
    st.strategy_history.append({"strategy": d.get("strategy"), "at_node": d.get("at_node"),
                                "ctx": d.get("ctx")})

def _on_hypothesis_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT board prioritization (audit-only): the engine recorded how the world model
    # ordered the OPEN hypotheses (order of ids + confidence + analysis trace). Latest-wins
    # (like policy_scores); `_derive_hypotheses` stamps each card's `priority` from `order`.
    n = _node_for_event(st, d)
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    st.hypothesis_ranking = d

def _on_rung_promoted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})

def _on_agent_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
    # chose and why. NEVER drives selection — the effect is the subsequent node_created,
    # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
    st.agent_decisions.append(d)

def _on_reward_hack_suspected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    record = {"node_id": nid, "signals": d.get("signals", []),
              "evidence_version": d.get("evidence_version", 0),
              "code_digest": d.get("code_digest")}
    if n is not None:
        record["generation"] = n.attempt
    st.reward_hacks.append(record)

def _on_foresight_selected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT predict-before-execute pick (audit-only). Kept so the world model can be
    # primed with its OWN calibration (did the picked node beat its parent?), closing the
    # predict→outcome loop. Store only the small fields the scoreboard needs; never selection.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    if nid is not None:
        record = {"node_id": nid, "confidence": d.get("confidence")}
        if generation is not _MISSING:
            record["generation"] = generation
        st.foresight_selected.append(record)

def _on_novelty_rejected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)

def _on_novelty_graded(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_grades.append(d)   # D3: a graded-ALLOW (level-4/5) the flat gate would reject (audit)

def _on_cross_run_prior(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.cross_run_priors.append(d)   # §21.20 Step 2: concept tried in a SIMILAR earlier run (audit; surface)

def _on_hypothesis_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1+: engine-written agentic merge — fold alias hypotheses into a canonical. Collected
    # here, APPLIED deterministically in `_derive_hypotheses` (no LLM in the fold). A malformed
    # entry is tolerated there; unknown on old logs -> skipped by the outer dispatch.
    if d.get("canonical") and d.get("aliases"):
        st.hypotheses_merged.append(d)

def _on_hypothesis_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1: an explicitly-registered hypothesis (human `add_hypothesis`, or a deep-research
    # direction) — may have no evidence yet. Evidence + verdict are DERIVED post-loop.
    if d.get("statement"):
        st.hypotheses_added.append(d)
        # Re-adding an abandoned statement reopens it (last write wins).
        try:
            hid = str(d.get("id") or hypothesis_id(str(d["statement"])))
            if hid in st.hypotheses_abandoned:
                st.hypotheses_abandoned.remove(hid)
        except Exception:
            pass

_CARD_REPLAY_ID_MAX = 256
_CARD_REPLAY_STATEMENT_MAX = 4_000
_CARD_REPLAY_SOURCE_MAX = 64
_CARD_REPLAY_RATIONALE_MAX = 400
_CARD_REPLAY_ACTION_MAP_MAX = 64
_CARD_REPLAY_ACTION_LIST_MAX = 64
_CARD_REPLAY_MERGE_ALIASES_MAX = 256
_CARD_REPLAY_NODE_ID_MAX = (1 << 31) - 1


def _card_replay_id(value) -> str | None:
    """Return one canonical card id without copying an oversized hostile string."""
    if not isinstance(value, str) or len(value) > _CARD_REPLAY_ID_MAX:
        return None
    bounded = value.strip()
    return bounded if bounded and bounded.isprintable() else None


def _card_replay_text(
    value, *, max_chars: int, strip: bool = False, allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str) or len(value) > max_chars:
        return None
    bounded = value.strip() if strip else value
    return bounded if bounded or allow_empty else None


def _card_replay_node_id(value) -> int | None:
    node_id = _coerce_node_id({"node_id": value})
    return node_id if node_id is not None and 0 <= node_id <= _CARD_REPLAY_NODE_ID_MAX else None


def _bounded_card_action_map(value) -> dict[str, float]:
    """Normalize a scalar map with lexical top-K identity and O(K) temporary memory."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        if (not isinstance(raw_key, str) or not raw_key or len(raw_key) > 200
                or isinstance(raw_value, bool) or not isinstance(raw_value, (int, float))):
            continue
        try:
            number = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(number):
            continue
        if len(out) >= _CARD_REPLAY_ACTION_MAP_MAX:
            greatest = max(out)
            if raw_key >= greatest:
                continue
            del out[greatest]
        out[raw_key] = number
    return dict(sorted(out.items()))


def _bounded_card_action_space(value) -> dict[str, list[float]]:
    """Normalize a search space without sorting/copying an attacker-sized mapping."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[float]] = {}
    rows = heapq.nsmallest(
        _CARD_REPLAY_ACTION_MAP_MAX,
        ((key, raw) for key, raw in value.items()
         if isinstance(key, str) and key and len(key) <= 200),
        key=lambda row: row[0],
    )
    for raw_key, raw_values in rows:
        if not isinstance(raw_values, list):
            continue
        values: list[float] = []
        for raw_value in raw_values[:_CARD_REPLAY_ACTION_LIST_MAX]:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                continue
            try:
                number = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                values.append(number)
        out[raw_key] = values
    return out


def _bounded_card_action(value: dict, *, record_unknown_fields: bool = False) -> dict:
    """Copy only the action fields consumed by ``_card_added_snapshot``."""
    out: dict = {}
    operator = _card_replay_text(value.get("operator"), max_chars=64, strip=True)
    if operator is not None:
        out["operator"] = operator
    if isinstance(value.get("params"), dict):
        out["params"] = _bounded_card_action_map(value["params"])
    if isinstance(value.get("space"), dict):
        out["space"] = _bounded_card_action_space(value["space"])
    profile = _card_replay_text(value.get("eval_profile"), max_chars=256, allow_empty=True)
    if profile is not None:
        out["eval_profile"] = profile

    concept_key = "concept_tags" if "concept_tags" in value else "concepts" if "concepts" in value else None
    if concept_key is not None:
        raw_concepts = value.get(concept_key)
        concepts, overflow, invalid = bounded_raw_concept_values(raw_concepts)
        if isinstance(raw_concepts, list):
            out[concept_key] = concepts
        # These flags are produced here, never trusted from the event. They retain the truth that a
        # compact membership is only a projection rather than forging a complete proposal receipt.
        out["_concept_tags_overflow"] = overflow
        out["_concept_tags_invalid"] = invalid

    if isinstance(value.get("parent_ids"), list):
        parent_ids: list[int] = []
        for raw_parent in value["parent_ids"][:_CARD_REPLAY_ACTION_LIST_MAX]:
            parent_id = _card_replay_node_id(raw_parent)
            if parent_id is not None and parent_id not in parent_ids:
                parent_ids.append(parent_id)
        out["parent_ids"] = parent_ids
    parent_id = _card_replay_node_id(value.get("parent_id"))
    if parent_id is not None:
        out["parent_id"] = parent_id
    if record_unknown_fields:
        known_fields = {
            "operator", "params", "space", "eval_profile", "concept_tags", "concepts",
            "parent_id", "parent_ids",
        }
        if any(field not in known_fields for field in value):
            # CODEX AGENT: retain only the fact that executable meaning was discarded. Copying an
            # unknown value would defeat the replay bound; forgetting its existence could turn a
            # lossy future-schema action into a receipt-backed selectable Card.
            out["_unknown_action_fields"] = True
    return out


def _bounded_card_ownership_receipt(value, *, card_id: str | None) -> dict | None:
    """Retain one exact, constant-size v1 ownership proof and reject every extension."""
    keys = {"v", "card_id", "action_digest"}
    if not isinstance(value, dict) or set(value) != keys or card_id is None:
        return None
    digest = value.get("action_digest")
    prefix = "card-action:v1:"
    if (type(value.get("v")) is not int or value["v"] != 1
            or value.get("card_id") != card_id
            or not isinstance(digest, str)
            or len(digest) != len(prefix) + 64
            or not digest.startswith(prefix)
            or any(char not in "0123456789abcdef" for char in digest[len(prefix):])):
        return None
    return {"v": 1, "card_id": card_id, "action_digest": digest}


def _bounded_card_added_receipt(d: dict) -> dict | None:
    """Canonical replay input for a ``card_added`` envelope."""
    rec: dict = {}
    card_id = _card_replay_id(d.get("id"))
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if card_id is None and statement is None:
        return None
    if card_id is not None:
        rec["id"] = card_id
    if statement is not None:
        rec["statement"] = statement
    source = _card_replay_text(d.get("source"), max_chars=_CARD_REPLAY_SOURCE_MAX, strip=True)
    if source is not None:
        rec["source"] = source
    rationale = _card_replay_text(d.get("rationale"), max_chars=_CARD_REPLAY_RATIONALE_MAX)
    if rationale is not None:
        rec["rationale"] = rationale
    at_node = _card_replay_node_id(d.get("at_node"))
    if at_node is not None:
        rec["at_node"] = at_node

    if isinstance(d.get("idea"), dict):
        # An explicit (even empty) idea owns the snapshot in historical replay; retaining that shape keeps
        # a top-level fallback action from silently overriding it after sanitization.
        rec["idea"] = _bounded_card_action(d["idea"], record_unknown_fields=True)
    else:
        rec.update(_bounded_card_action(d))

    if isinstance(d.get("parent_ids"), list):
        parent_ids: list[int] = []
        for raw_parent in d["parent_ids"][:_CARD_REPLAY_ACTION_LIST_MAX]:
            parent_id = _card_replay_node_id(raw_parent)
            if parent_id is not None and parent_id not in parent_ids:
                parent_ids.append(parent_id)
        rec["parent_ids"] = parent_ids
    parent_id = _card_replay_node_id(d.get("parent_id"))
    if parent_id is not None:
        rec["parent_id"] = parent_id
    scored_against = _card_replay_node_id(d.get("scored_against"))
    if scored_against is not None:
        rec["scored_against"] = scored_against
    footprint = normalize_researcher_footprint(d.get("footprint"))
    if footprint is not None:
        rec["footprint"] = footprint
    if isinstance(d.get("steering_context"), list):
        steering: list[dict] = []
        budget = [256]
        for item in d["steering_context"][:_CARD_REPLAY_ACTION_LIST_MAX]:
            if not isinstance(item, dict):
                continue
            valid, bounded = _bounded_card_enrichment(item, budget=budget)
            if valid and isinstance(bounded, dict):
                steering.append(bounded)
        rec["steering_context"] = steering
    ownership_receipt = _bounded_card_ownership_receipt(
        d.get("ownership_receipt"), card_id=card_id)
    if ownership_receipt is not None:
        rec["ownership_receipt"] = ownership_receipt
    return rec


def _bounded_card_merge_receipt(d: dict) -> dict | None:
    canonical = _card_replay_id(d.get("canonical"))
    raw_aliases = d.get("aliases")
    if canonical is None or not isinstance(raw_aliases, list) or not raw_aliases:
        return None
    aliases: list[str] = []
    for raw_alias in raw_aliases[:_CARD_REPLAY_MERGE_ALIASES_MAX]:
        alias = _card_replay_id(raw_alias)
        if alias is not None and alias not in aliases:
            aliases.append(alias)
    if not aliases:
        return None
    rec = {"canonical": canonical, "aliases": aliases}
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if statement is not None:
        rec["statement"] = statement
    return rec


def _bounded_card_drop_receipt(d: dict) -> dict | None:
    card_id = _card_replay_id(d.get("id"))
    if card_id is None:
        return None
    rec = {"id": card_id}
    reason = _card_replay_text(d.get("reason"), max_chars=_CARD_REPLAY_RATIONALE_MAX)
    if reason is not None:
        rec["reason"] = reason
    raw_dropped_by = d.get("dropped_by")
    if raw_dropped_by is None or raw_dropped_by == "":
        raw_dropped_by = d.get("by")
    dropped_by = _card_replay_text(
        raw_dropped_by, max_chars=_CARD_REPLAY_SOURCE_MAX, strip=True)
    if dropped_by is not None:
        rec["dropped_by"] = dropped_by
    return rec


def _on_card_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Hypothesis-card Kanban (docs/23): bounded registration plus an optional immutable ownership
    # receipt. Unreceipted historical rows remain visible shadows; only `_derive_cards` may validate
    # native identity/readiness. Evidence/verdict/status are derived.
    receipt = _bounded_card_added_receipt(d)
    if receipt is not None:
        # CODEX AGENT: RunState is deep-copied on every incremental snapshot. Never retain Event.data
        # here: one unknown megabyte field would otherwise be multiplied by every live state read.
        st.cards_added.append(receipt)

def _on_card_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Engine-written agentic merge — fold alias cards into a canonical. Collected here, APPLIED
    # deterministically in `_derive_cards` (no LLM in the fold), order-tolerant, back-compat on old logs.
    receipt = _bounded_card_merge_receipt(d)
    if receipt is not None:
        # CODEX AGENT: aliases are identity-bearing, so cap the durable prefix before RunState owns it;
        # unknown merge metadata has no replay semantics and remains only in the append-only log.
        st.cards_merged.append(receipt)

def _on_card_dropped(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Engine/operator drop of a card: {id, reason, dropped_by}. Lifecycle override applied in
    # `_derive_cards` (the card stays visible with status='dropped', like an abandoned hypothesis).
    receipt = _bounded_card_drop_receipt(d)
    if receipt is not None:
        # CODEX AGENT: keep a typed lifecycle receipt, not the raw control payload. This also prevents
        # arbitrary objects from becoming enormous strings later in `_derive_cards`.
        st.cards_dropped.append(receipt)

def _on_card_enriched(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Layer 1b: a delta onto a card (novelty verdict, cross-run prior, footprint-finalize, steering cues).
    # Collected here; APPLIED last-write-by-envelope-order in `_derive_cards`.
    raw_id = d.get("id")
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id.strip()) <= 256:
        rec = {"id": raw_id.strip()}
        allowed = (
            "novelty_verdict", "cross_run_prior", "footprint", "steering_context",
            "concept_tags", "lesson_refs", "claim_refs", "research_origin",
            "foresight_rank", "confidence",
        )
        # CODEX AGENT: bound each allow-listed sibling independently. A huge lexically-early unknown
        # field must not consume a shared budget and erase id or a later valid field.
        for key in allowed:
            if key not in d:
                continue
            if key == "concept_tags":
                # CODEX AGENT: keep enough derived receipt data to say that a node-less enrichment was
                # lossy.  The caller-provided provenance_tier is intentionally not copied: a free-form
                # delta must never promote its own tags to classifier/operator truth.
                if not isinstance(d[key], list):
                    continue
                values, overflow, invalid = bounded_raw_concept_values(d[key])
                rec[key] = values
                rec["_concept_tags_overflow"] = overflow
                rec["_concept_tags_invalid"] = invalid
                continue
            valid, bounded = _bounded_card_enrichment(d[key])
            if valid:
                rec[key] = bounded
        # CODEX AGENT: envelope seq is authoritative; physical order is the deterministic tie-break for
        # legacy/default envelopes. Assign both after copying so payload fields can never spoof ordering.
        rec["_seq"] = e.seq if type(e.seq) is int else -1
        rec["_event_index"] = ctx.event_index if type(ctx.event_index) is int else -1
        st.cards_enriched.append(rec)

def _on_card_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Layer 1b: FOREAGENT board prioritization for cards — latest wins (mirrors `_on_hypothesis_ranked`).
    raw_order = d.get("order")
    order: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_order, list):
        for raw in raw_order[:256]:
            if not isinstance(raw, str):
                continue
            cid = raw.strip()
            if not cid or len(cid) > 256 or cid in seen:
                continue
            seen.add(cid)
            order.append(cid)
    # CODEX AGENT: a malformed/future order is an honest empty ranking, never an iterable assumption
    # that can brick replay. Preserve metadata while replacing only the bounded, deduplicated order.
    metadata: dict = {"order": order}
    raw_at_node = d.get("at_node")
    if type(raw_at_node) is int and 0 <= raw_at_node <= (1 << 31) - 1:
        metadata["at_node"] = raw_at_node
    raw_confidence = d.get("confidence")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError, OverflowError):
        confidence = math.nan
    if (not isinstance(raw_confidence, bool) and math.isfinite(confidence)
            and 0.0 <= confidence <= 1.0):
        metadata["confidence"] = confidence
    if isinstance(d.get("reason"), str):
        metadata["reason"] = d["reason"][:400]
    if isinstance(d.get("ranked"), list):
        valid, ranked = _bounded_card_enrichment(d["ranked"])
        if valid:
            metadata["ranked"] = ranked
    st.card_ranking = metadata

def _on_hypothesis_updated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Carries a status override (human/agent drops — or reopens — a line of inquiry).
    # Last write wins: "deleted" removes the card entirely (sticky); "abandoned" adds the
    # abandoned override; any other status clears the abandoned override (reopen).
    hid = d.get("id")
    if hid:
        status = d.get("status")
        if status == "deleted":
            if hid not in st.hypotheses_deleted:
                st.hypotheses_deleted.append(hid)
        elif status == "abandoned":
            if hid not in st.hypotheses_abandoned:
                st.hypotheses_abandoned.append(hid)
        elif hid in st.hypotheses_abandoned:
            st.hypotheses_abandoned.remove(hid)

def _on_proxy_scored(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    if nid is not None and d.get("score") is not None:
        st.proxy_scores[nid] = d["score"]
    if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
        st.proxy_skipped.append(nid)

def _on_node_verified(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1-c: freeze a node's calibrated §12-verifier soundness score (the LLM output can't be recomputed
    # in the deterministic fold). Generation-scoped exactly like proxy_scored: a score computed against a
    # reset-abandoned attempt (stale generation) is dropped, so a stale-attempt verification can't bias
    # selection. Audit sidecar — read ONLY as a metric-tie-break in _select_best; never a raw override.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is None or n.id in st.aborted_nodes or n.tombstoned
            or n.status is not NodeStatus.evaluated):
        return
    # node_verified is a BRAND-NEW selection-affecting event — no legacy log carries it, and the engine
    # always stamps `generation` (n.attempt) at emit — so REQUIRE the stamp (reject a missing OR mismatched
    # generation) rather than accept-a-missing-one as current. A forged/hand-edited unscoped score can't
    # then bias selection; this is strictly tighter than the additive-legacy pattern the older per-node
    # events must keep for their pre-generation logs.
    if _event_generation(d) is _MISSING or not _generation_matches(n, d):
        return
    evidence_digest = d.get("evidence_digest")
    if evidence_digest is None:
        # Digestless rows are a legacy raw-metric format.  Once confirmation or holdout data exists, the
        # evidence has a revision identity and an in-flight legacy row cannot be allowed to restore a score
        # invalidated by that newer evidence.
        if n.confirmed_mean is not None or n.holdout_metric is not None:
            return
    elif (not isinstance(evidence_digest, str)
          or evidence_digest != verifier_evidence_digest(st.direction, n)):
        return
    score = d.get("score")
    if is_usable_metric(score) and 0.0 <= float(score) <= 1.0:
        n.verifier_score = float(score)


def _on_verifier_group_scored(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Publish a complete verifier tie treatment only after every member validates."""
    # This event is atomic and selection-affecting; reject the entire record unless
    # version, contract,
    # membership, generation and evidence revision all match the current selector-visible tie group.
    if (not st.select_verifier_tiebreak or isinstance(d.get("v"), bool) or d.get("v") != 1
            or d.get("contract") != VERIFIER_SELECTION_CONTRACT
            or d.get("contract") != st.select_verifier_contract):
        return
    requested = d.get("requested_samples")
    if (isinstance(requested, bool) or not isinstance(requested, int)
            or requested != st.select_verifier_samples):
        return
    members = d.get("members")
    if not isinstance(members, list) or not 2 <= len(members) <= 8:
        return
    seen: set[int] = set()
    staged: list[tuple[Node, float]] = []
    for row in members:
        if not isinstance(row, dict):
            return
        nid = _coerce_node_id(row)
        node = st.nodes.get(nid) if nid is not None else None
        if (node is None or nid in seen or node.id in st.aborted_nodes or node.tombstoned
                or node.status is not NodeStatus.evaluated):
            return
        if _event_generation(row) is _MISSING or not _generation_matches(node, row):
            return
        digest = row.get("evidence_digest")
        if not isinstance(digest, str) or digest != verifier_evidence_digest(st.direction, node):
            return
        score, n_samples, agreement = row.get("score"), row.get("n_samples"), row.get("agreement")
        if not is_usable_metric(score) or not 0.0 <= float(score) <= 1.0:
            return
        if (isinstance(n_samples, bool) or not isinstance(n_samples, int)
                or not 1 <= n_samples <= requested or n_samples * 2 <= requested):
            return
        if not is_usable_metric(agreement) or not 0.5 < float(agreement) <= 1.0:
            return
        method = row.get("method")
        if not isinstance(method, str) or len(method) > 80:
            return
        seen.add(nid)
        staged.append((node, float(score)))
    expected = {frozenset(node.id for node in group) for group in verifier_tie_groups(st)}
    # Member validity is insufficient; this must be the complete selector-reachable tie-set.
    # Reject a well-formed subset, a losing tie, or a mean group shadowed by a non-empty holdout pool before
    # publishing any score, so a forged/torn record cannot steer a different comparison.
    if frozenset(seen) not in expected:
        return
    for node, score in staged:
        node.verifier_score = score

def _on_best_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1 epoch identity: a confirmation certificate authorizes selection state (confirmed_done + the
    # confirm-override in _select_best), so it must be bound to the candidate-set epoch it was computed
    # against. A best_confirmed STAMPED with a stale epoch — e.g. an in-flight confirm pass that appends
    # AFTER a cross-writer reopen bumped search_epoch — is rejected, so an epoch-(N-1) confirmation can't
    # authorize state a fresh epoch N must re-decide. Additive/reader-defaulted: a missing stamp (legacy
    # logs / manual events) is treated as legacy-current, so old logs fold byte-identically. The
    # requeuing-reopen case is already caught by _generation_map_matches; this closes the NON-requeuing
    # reopen (no disclosed holdout), which leaves generations unchanged but still bumps the epoch.
    # This boolean changes whether confirmation can override the verifier's CI-tie winner.  Do not coerce
    # strings/numbers by truthiness: a malformed certificate is rejected as a whole and cannot even close
    # the confirmation gate.  Absence remains the legacy `True` default.
    if "significant" in d and not isinstance(d.get("significant"), bool):
        return
    if "search_epoch" in d and d.get("search_epoch") != st.search_epoch:
        return
    if not _generation_map_matches(st, d):
        return
    nid = _coerce_node_id(d)
    if "node_id" in d:
        ctx.best_confirmed = nid
        # R1-d: record whether this certificate is a SIGNIFICANT winner (default True: legacy events with no
        # `significant` field keep the unconditional override). A non-significant certificate is a STATISTICAL
        # tie the verifier CI-tie may resolve instead — see _select_best.
        ctx.best_confirmed_significant = d.get("significant", True)
    st.confirmed_done = True   # the confirmation phase ran to completion

def _on_run_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    accepted_after_seq: int | None = None
    if "after_seq" in d:
        raw = d.get("after_seq")
        if isinstance(raw, bool):
            return
        try:
            after_seq = int(raw)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return                    # an external event won the decision→finish race
        accepted_after_seq = after_seq
    pending = ctx.pending_finish_report
    if pending is not None:
        report_seq, report_index, report = pending
        # Modern events bind the report seq into run_finished.after_seq. Historical emitters had no
        # CAS payload, so accept only a physically adjacent report->finish pair. An intervening event,
        # including an unknown forward-compatible one, leaves the provisional narrative unpublished.
        modern_adjacent = accepted_after_seq is not None and report_seq == accepted_after_seq
        legacy_adjacent = (accepted_after_seq is None
                           and ctx.event_index == report_index + 1)
        if modern_adjacent or legacy_adjacent:
            st.report = report
        ctx.pending_finish_report = None
    st.finished = True
    st.finalization_marker_seq = None
    if e.seq is not None:
        st.last_finish_seq = e.seq
        # Recovery is explicitly opted into by modern finish events. Markerless historical finishes
        # were already complete before this protocol existed and must never become synthetic work.
        if not bool(d.get("finalization_required", False)):
            st.finalized_finish_seq = e.seq
    st.stop_reason = d.get("reason")
    # Drop any dangling "building" marker(s): if a dev session died mid-build (no node_created /
    # node_failed) the marker would otherwise persist, and the UI would show a breathing
    # "building…" card + a false "working" pulse on a run that is over.
    st.building = None
    st.buildings.clear()


def _on_finalization_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    raw = d.get("finish_seq")
    if isinstance(raw, bool):
        return
    try:
        finish_seq = int(raw)
    except (TypeError, ValueError, OverflowError):
        return
    if (st.finished and finish_seq == st.last_finish_seq
            and st.finalized_finish_seq != finish_seq):
        st.finalized_finish_seq = finish_seq
        st.finalization_marker_seq = e.seq

def _on_resume_or_run_reopened(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # RESUME (the one operator "continue"): lift EVERY stopped state so re-entering the loop
    # keeps going — whether the run was PAUSED (stop, no finalize), ABORTED (finalize →
    # stop_requested → run_finished), or naturally FINISHED (budget exhausted, then reopened
    # with more budget). Clears paused + finished + stop_requested + stop_reason. Deterministic
    # under replay — a later run_finished simply sets `finished` again. EV_RUN_REOPENED is the
    # legacy alias of RESUME (kept so old logs + the UI's reopen path fold identically); the two
    # 3-verb operator controls are `stop` (EV_PAUSE) and `finalize` (EV_RUN_ABORT).
    #
    # P0-2 search epoch: reopening a run that had already FINISHED (its confirmation/approval
    # promotion completed for the prior candidate set) begins a NEW search epoch. Any nodes added
    # after the reopen are a fresh candidate set, so the prior COMPLETION gates must not carry over:
    # clear `confirmed_done` (so the confirm phase re-runs and can confirm a better new candidate —
    # already-confirmed nodes are cheaply reused via their memoized `confirmed_mean`) and re-open
    # approval (so the possibly-new best is re-ratified rather than inheriting the old grant). A
    # resume from a mere PAUSE (finished never set) is the SAME epoch and leaves these gates intact.
    # Checked BEFORE clearing `finished` below. Back-compat: old logs without a reopen-after-finish
    # keep search_epoch=0 and fold identically.
    if st.finished or st.holdout_evaluated_ids:
        if st.holdout_evaluated_ids:
            # F2: requeue-with-metric-wipe only for an epoch-aware (modern) disclosure; a legacy
            # holdout log rotates without wiping surviving incumbents (invariant 5b).
            _rotate_search_epoch(st, requeue_partition_scores=st.holdout_epoch_aware)
        else:
            _rotate_search_epoch(st, requeue_partition_scores=False)
        # A reopen begins a new candidate epoch, so the prior epoch's confirmation certificate must not
        # keep authorizing selection. Clear BOTH the folded flag AND the threaded ctx.best_confirmed the
        # `_select_best` confirm-override reads — every other invalidation site (node_reset, tombstone,
        # new-candidate) pairs these two, and omitting the ctx clear here let an epoch-(N-1) certificate
        # keep overriding epoch-N's metric winner after confirmed_done reset.
        st.confirmed_done = False
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
        # P0-2 freshly-hidden per-epoch holdout: the prior epoch's holdout was DISCLOSED at the
        # finish (its scores drove the champion pick), so the reopened epoch must NOT re-score its
        # new candidates on that same partition — the engine rebuilds `_holdout_idx` for the new
        # epoch (a different, never-disclosed split). Clear the gate + the now-stale holdout metrics
        # so the holdout phase re-runs and re-scores every current leader on the fresh split (keeping
        # the champion comparable on ONE holdout). New holdout_evaluated events carry the new epoch;
        # a late one stamped with the prior epoch is dropped by the epoch guard in _on_holdout_evaluated.
    st.paused = False
    st.pause_node_id = None
    st.pause_generation = None
    st.finished = False
    st.stop_reason = None
    st.stop_requested = None

# --- live operator control events (UI intervention). Intent only; the engine reads
# these and writes the matching domain effect. Deterministic under replay. ---
def _on_resume_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1 durable resume intent: record the request seq + time. A request seq newer than the last
    # `resume_served` (below) is an unfulfilled resume the reconciler re-spawns. Monotonic by seq, so a
    # duplicate/out-of-order fold is idempotent; the ts is the request event's own recorded time.
    if e.seq > st.last_resume_request_seq:
        st.last_resume_request_seq = e.seq
        st.last_resume_request_ts = float(getattr(e, "ts", 0.0) or 0.0)
        mode = d.get("mode")
        if mode in ("resume", "finalize"):
            st.last_resume_request_mode = mode
        elif not d.get("launch_claim"):
            # A real legacy request means ordinary resume. A claim-only record is transport metadata
            # and must preserve the pending intent's mode (especially finalize).
            st.last_resume_request_mode = "resume"
    if d.get("launch_claim") and e.seq > st.last_resume_launch_seq:
        st.last_resume_launch_seq = e.seq
        st.last_resume_launch_ts = float(getattr(e, "ts", 0.0) or 0.0)

def _on_resume_served(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1: the engine acquired the singleton lock and is driving the loop -> every resume requested
    # before this seq is fulfilled. Seq-gated so one serve satisfies several piled-up requests.
    if e.seq > st.last_resume_served_seq:
        st.last_resume_served_seq = e.seq
        if st.finished and st.last_resume_request_mode == "finalize":
            # A finalize hand-off that arrived after run_finished repairs/acknowledges the existing
            # wrap-up; it must not create a second finish. Consume its lingering stop intent once the
            # finalize-mode CLI actually owns the singleton lock.
            st.stop_requested = None

def _on_run_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FINALIZE: the loop turns stop_requested into a run_finished (which runs the end-of-run
    # finalization — report/lessons/case/cost). A bare `stop` uses EV_PAUSE instead (no finalize).
    st.stop_requested = d.get("reason", "operator")
    if e.seq is not None:
        st.last_stop_request_seq = e.seq

def _on_pause(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # STOP: freeze WITHOUT finalizing (finalize.py gates the wrap-up on `finished`, which a pause
    # never sets). A later `finalize` (EV_RUN_ABORT) can still wrap it up; RESUME lifts it.
    previous = (st.paused, st.pause_node_id, st.pause_generation)
    if d.get("node_id") is not None:
        # A human STOP is stronger than the scoped developer-crash circuit breaker. If the operator
        # paused while a build was still failing, the later automatic pause must not take ownership:
        # node reset/abort may clear only an auto-pause, never the explicit operator stop.
        if st.paused and st.pause_node_id is None:
            return
        nid = _coerce_node_id(d)
        n = st.nodes.get(nid) if nid is not None else None
        if (n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)
                or n.status is not NodeStatus.failed or n.error_reason != "developer_crash"):
            return
        st.pause_node_id = nid
        st.pause_generation = n.attempt
    else:
        st.pause_node_id = None
        st.pause_generation = None
    st.paused = True
    if previous != (st.paused, st.pause_node_id, st.pause_generation):
        st.pause_event_seq = e.seq


def _on_restart(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one durable, server-owned pause -> replacement-owner handoff.

    The old engine observes the operator pause and releases its singleton lock. At the same time the
    event itself is the resume request watermark, so losing the browser, command worker, or whole UI
    server cannot strand the run: the normal startup reconciler can claim and launch it. A replacement
    CLI clears the pause with ``resume`` and appends ``resume_served`` only after acquiring the lock.
    """
    _on_pause(st, e, {}, ctx)
    if e.seq > st.last_resume_request_seq:
        st.last_resume_request_seq = e.seq
        st.last_resume_request_ts = float(getattr(e, "ts", 0.0) or 0.0)
        st.last_resume_request_mode = "resume"

def _on_node_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = n is None and _event_generation(d) is _MISSING
    if (nid is not None
            and (legacy_unknown or (n is not None and _control_generation_matches(n, d)))
            and nid not in st.aborted_nodes):
        if n is not None:
            _remove_current_failure(st, n)
        st.aborted_nodes.append(nid)
        if n is not None:
            n.rerun_from = None
            n.rerun_stage = None
        _clear_build_marker(st, d, nid)
        if st.pause_node_id == nid:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
        st.ablate_requests = [queued for queued in st.ablate_requests if queued != nid]
        st.ablate_request_generations = [
            r for r in st.ablate_request_generations if r.get("node_id") != nid]
        st.confirm_requests = [queued for queued in st.confirm_requests if queued != nid]
        st.confirm_request_generations = [
            r for r in st.confirm_request_generations if r.get("node_id") != nid]
        if st.approval_subject == nid or st.approved_node_id == nid:
            st.awaiting_approval = False
            st.approved = False
            st.approval_subject = None
            st.approval_generation = None
            st.approved_node_id = None
        if st.champion == nid:
            st.champion = None
        if st.finished:
            if ctx.best_confirmed == nid:
                ctx.best_confirmed = None
            return
        st.confirmed_done = False
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
        _invalidate_disclosed_holdout(st)

def _on_budget_extend(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # max_seconds / max_eval_seconds are ABSOLUTE new ceilings (last write wins). add_nodes is
    # an ADDITIVE delta — "give the run N more nodes" — so several extensions accumulate; the
    # orchestrator folds it into the policy's effective max_nodes so a finished run, once
    # reopened, proposes more experiments instead of immediately re-finishing.
    # max_seconds/max_eval_seconds (budgets) + timeout/the two parallel axes are ABSOLUTE new
    # values (last write wins). Canonical and legacy parallel spellings remain replay-compatible.
    # COERCE to number in the fold: a UI form / TUI can post a STRING ("600"), and the engine
    # compares these numerically (`total_eval_seconds >= max_es`), so an un-coerced string would
    # raise TypeError in the main loop — and because the event replays, EVERY resume re-crashes
    # (a permanent poison event). A non-numeric value is skipped, not stored.
    for _k in ("max_seconds", "max_eval_seconds", "timeout"):
        _raw = d.get(_k)
        if _raw is None or isinstance(_raw, bool):
            continue
        try:
            _v = float(_raw)
        except (TypeError, ValueError, OverflowError):
            continue
        # CODEX AGENT: malformed historical control events must remain total under replay. Reject
        # non-finite/non-positive ceilings instead of persisting a resume-crashing poison value.
        if math.isfinite(_v) and _v > 0:
            st.budget_overrides[_k] = _v
    for _legacy, _canonical, _upper in (
            ("max_parallel", "eval_parallel", 1024),
            ("parallel_build", "llm_parallel", 64)):
        _selected: tuple[str, int] | None = None
        # Legacy first, canonical last: canonical wins when one event carries both valid spellings.
        # Across events, whichever spelling arrived last owns the whole axis family and removes the
        # stale sibling; otherwise apply's canonical-last order could resurrect an older value.
        for _k in (_legacy, _canonical):
            _raw = d.get(_k)
            if _raw is None or isinstance(_raw, bool):
                continue
            if isinstance(_raw, float) and (
                    not math.isfinite(_raw) or not _raw.is_integer()):
                continue
            try:
                _v = int(_raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if 0 <= _v <= _upper:
                _selected = (_k, _v)
        if _selected is not None:
            _key, _value = _selected
            # CODEX AGENT: one folded key per authority family preserves true event-order LWW while
            # retaining the latest event's spelling for old/no-broker resume compatibility.
            st.budget_overrides.pop(
                _legacy if _key == _canonical else _canonical, None)
            st.budget_overrides[_key] = _value
            if _canonical == "llm_parallel" and _key == _canonical:
                # CODEX AGENT: the legacy alias historically governed only build fan-out. Preserve the
                # last explicit canonical shared-total intent independently, so canonical->legacy
                # sequences behave identically before and after process restart without retroactively
                # throttling legacy-only logs.
                st.budget_overrides["llm_broker_total"] = _value
    _raw_add = d.get("add_nodes")
    if _raw_add is not None and not isinstance(_raw_add, bool):
        if not (isinstance(_raw_add, float) and (
                not math.isfinite(_raw_add) or not _raw_add.is_integer())):
            try:
                _add = int(_raw_add)
                if 0 < _add <= 1_000_000:
                    st.budget_overrides["add_nodes"] = (
                        int(st.budget_overrides.get("add_nodes", 0)) + _add)
            except (TypeError, ValueError, OverflowError):
                pass

def _on_hint(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only by default; a `replace` hint supersedes all prior standing directives
    # (mirrors set_strategy/pending_strategy) so the boss can rewrite the single directive
    # instead of accumulating contradictory ones. Replay-safe: deterministic over the log.
    if d.get("replace"):
        st.pending_hints = [d]
    else:
        st.pending_hints.append(d)

def _on_set_strategy(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
    # engine applies it before consulting the Strategist, so a human always wins. The pin owns
    # only the fields it names (policy/policy_params/fidelity) and STAYS in force for the rest
    # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
    # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
    st.pending_strategy = d.get("strategy")

def _on_force_confirm(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        st.confirm_requests.append(nid)
        st.confirm_request_generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.confirm_requests.append(nid)   # legacy queued-before-create intent

def _on_force_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        st.ablate_requests.append(nid)
        st.ablate_request_generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.ablate_requests.append(nid)    # legacy queued-before-create intent

def _on_fork(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d, "from_node_id")
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        record = dict(d)
        record["from_node_id"] = nid
        record.setdefault("generation", n.attempt)
        st.fork_requests.append(record)
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.fork_requests.append(dict(d))  # legacy queued-before-create intent

def _on_fork_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.forks_done += 1   # one per processed fork request (gate for replay-safe fulfillment)

def _on_inject_node(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.inject_requests.append(d)        # operator-authored experiment (manual tree edit)

def _on_inject_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.injects_done += 1                 # one per processed inject (replay-safe gate)

def _on_deep_research(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.research_requests.append(d)       # manual "go think hard" request (control event)

def _on_research_completed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Deep-Research memo (audit-only sidecar; NEVER touches nodes/best). `served_manual`
    # advances the manual-request gate so a resume never re-runs a served request.
    from looplab.core.advisory_payloads import sanitize_research_memo_payload
    # CODEX AGENT: old events predate D8 omission receipts. Preserve their replay shape (and unknown authority)
    # instead of manufacturing a complete receipt from an already-truncated legacy projection.
    st.research.append(sanitize_research_memo_payload(d.get("memo") or d, add_receipts=False))
    if d.get("served_manual"):
        st.research_served += 1

def _on_lessons_distilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # M6 mid-run comparative-lesson distillation (audit-only sidecar; NEVER touches
    # nodes/best). at_node + pair ids are the replay-safe gates (cadence + no re-distill).
    st.lessons_distilled.append(d)

def _on_lessons_refreshed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.lessons_refreshed.append(d)   # M6 shared-store re-read (audit-only cadence gate)

def _on_report_generated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Agent-authored run report (audit-only sidecar; NEVER touches nodes/best). Latest wins —
    # the cadence and manual-refresh paths both append this; the freshest narrative stands.
    from looplab.core.advisory_payloads import sanitize_report_payload
    content = sanitize_report_payload(d.get("content") or d)
    # The event envelope is the publication authority. Model/provider content must not forge which
    # node-count/trigger the writer bound, nor the physical receipt that made the narrative durable.
    # Preserve inner at_node/trigger only for historical events whose outer payload omitted them.
    if "at_node" in d or "trigger" in d:
        envelope = sanitize_report_payload({
            "at_node": d.get("at_node"), "trigger": d.get("trigger"),
        })
        if "at_node" in d:
            content["at_node"] = envelope["at_node"]
        if "trigger" in d:
            content["trigger"] = envelope["trigger"]
    content["published_seq"] = (e.seq if type(e.seq) is int
                                and 0 <= e.seq <= (1 << 53) - 1 else None)
    content["published_at"] = (float(e.ts) if type(e.ts) in (int, float)
                               and math.isfinite(e.ts) and 0 < e.ts <= 253_402_300_799
                               else None)
    if "trigger" in d and content["trigger"] == "finish":
        # Publish only if the immediately-adjacent run_finished accepts this report's CAS chain.
        ctx.pending_finish_report = (e.seq, ctx.event_index, content)
        return
    st.report = content

def _on_confirm_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)   # forced-confirm finished for this node (gate; selection untouched)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and nid not in st.aborted_nodes and _generation_matches(n, d)
            and nid not in st.confirmed_forced):
        st.confirmed_forced.append(nid)
    if n is not None and nid not in st.aborted_nodes and _generation_matches(n, d):
        key = {"node_id": nid, "generation": n.attempt}
        if key not in st.confirmed_forced_generations:
            st.confirmed_forced_generations.append(key)

def _on_annotation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # `annotation` is a sanctioned /control event appended VERBATIM, and `annotations` is keyed by int
    # node id (dict[int, list[str]]) — so a forged `{"node_id":[999]}` would make `setdefault` hash the
    # unhashable list and raise TypeError, bricking the fold (same class as the approval grant above).
    # `_coerce_node_id` guards the key (reject bool / unhashable / non-coercible) so it can never raise; a
    # null/garbage id simply drops the note.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    st.annotations.setdefault(nid, []).append(d.get("text", ""))
    if apply_comment_event(st.comments, e) is not None:
        st.comments_revision = e.seq


def _on_comment(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Collaboration is audit-only and selection-neutral.  The shared reducer applies only an exact
    # version chain and turns malformed/hand-authored records into deterministic no-ops.
    if apply_comment_event(st.comments, e) is not None:
        st.comments_revision = e.seq

def _on_promote(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = (n is None and nid not in st.aborted_nodes
                      and _event_generation(d) is _MISSING)
    if legacy_unknown or (n is not None and not n.tombstoned and nid not in st.aborted_nodes
                          and _control_generation_matches(n, d)):
        st.promotions.append(d)
        if d.get("alias", "champion") == "champion":
            st.champion = nid

# The dispatch registry — event type -> handler. Unknown types are absent: they no-op.
_HANDLERS = {
    EV_RUN_STARTED: _on_run_started,
    EV_TRUST_GATE_CHANGED: _on_trust_gate_changed,
    EV_NODE_BUILDING: _on_node_building,
    EV_NODE_CREATED: _on_node_created,
    EV_NODE_EVALUATED: _on_node_evaluated,
    EV_NODE_FAILED: _on_node_failed,
    EV_NODE_REPAIRED: _on_node_repaired,
    EV_NODE_TOMBSTONED: _on_node_tombstoned,
    EV_RESUME_REQUESTED: _on_resume_requested,
    EV_RESUME_SERVED: _on_resume_served,
    EV_RESTART: _on_restart,
    EV_NODE_RESET: _on_node_reset,
    EV_STAGE_FINISHED: _on_stage_finished,
    EV_CONFIRM_EVAL: _on_confirm_eval,
    EV_NODE_CONFIRMED: _on_node_confirmed,
    EV_HOLDOUT_EVALUATED: _on_holdout_evaluated,
    EV_AGENT_VALIDATED: _on_agent_validated,
    EV_DATA_PROFILED: _on_data_profiled,
    EV_DATA_PROVENANCE: _on_data_provenance,
    EV_HOST_GRADING: _on_host_grading,
    EV_SETUP_FINISHED: _on_setup_finished,
    EV_RUN_SETUP_FINISHED: _on_run_setup_finished,
    EV_DATA_LEAKAGE: _on_data_leakage,
    EV_APPROVAL_REQUESTED: _on_approval_requested,
    EV_APPROVAL_GRANTED: _on_approval_granted,
    EV_SPEC_PROPOSED: _on_spec_proposed,
    EV_SPEC_APPROVAL_REQUESTED: _on_spec_approval_requested,
    EV_SPEC_APPROVED: _on_spec_approved,
    EV_SPEC_DRIFT: _on_spec_drift,
    EV_WORKSPACE_CHANGED: _on_workspace_changed,
    EV_ENV_CHANGED: _on_env_changed,
    EV_DIVERSITY_ARCHIVE: _on_diversity_archive,
    EV_COVERAGE_SNAPSHOT: _on_coverage_snapshot,
    EV_CONCEPT_COVERAGE_SNAPSHOT: _on_concept_coverage_snapshot,
    EV_NODE_CONCEPTS: _on_node_concepts,
    EV_RUN_CONCEPTS: _on_run_concepts,
    EV_CONCEPT_TAG_EDITED: _on_concept_tag_edited,
    EV_HYPOTHESIS_CONCEPTS: _on_hypothesis_concepts,
    EV_CONCEPT_CONSOLIDATION: _on_concept_consolidation,
    EV_CONCEPT_EDGE: _on_concept_edge,
    EV_LLM_COST: _on_llm_cost,
    EV_LLM_USAGE: _on_llm_usage,
    EV_ABLATE: _on_ablate,
    EV_POLICY_DECISION: _on_policy_decision,
    EV_STRATEGY_DECISION: _on_strategy_decision,
    EV_HYPOTHESIS_RANKED: _on_hypothesis_ranked,
    EV_RUNG_PROMOTED: _on_rung_promoted,
    EV_AGENT_DECISION: _on_agent_decision,
    EV_REWARD_HACK_SUSPECTED: _on_reward_hack_suspected,
    EV_FORESIGHT_SELECTED: _on_foresight_selected,
    EV_NOVELTY_REJECTED: _on_novelty_rejected,
    EV_NOVELTY_GRADED: _on_novelty_graded,
    EV_CROSS_RUN_PRIOR: _on_cross_run_prior,
    EV_NODE_VERIFIED: _on_node_verified,
    EV_VERIFIER_GROUP_SCORED: _on_verifier_group_scored,
    EV_HYPOTHESIS_MERGED: _on_hypothesis_merged,
    EV_HYPOTHESIS_ADDED: _on_hypothesis_added,
    EV_HYPOTHESIS_UPDATED: _on_hypothesis_updated,
    EV_CARD_ADDED: _on_card_added,
    EV_CARD_MERGED: _on_card_merged,
    EV_CARD_DROPPED: _on_card_dropped,
    EV_CARD_ENRICHED: _on_card_enriched,
    EV_CARD_RANKED: _on_card_ranked,
    EV_PROXY_SCORED: _on_proxy_scored,
    EV_BEST_CONFIRMED: _on_best_confirmed,
    EV_RUN_FINISHED: _on_run_finished,
    EV_FINALIZATION_FINISHED: _on_finalization_finished,
    EV_RESUME: _on_resume_or_run_reopened,
    EV_RUN_REOPENED: _on_resume_or_run_reopened,
    EV_RUN_ABORT: _on_run_abort,
    EV_PAUSE: _on_pause,
    EV_NODE_ABORT: _on_node_abort,
    EV_BUDGET_EXTEND: _on_budget_extend,
    EV_HINT: _on_hint,
    EV_SET_STRATEGY: _on_set_strategy,
    EV_FORCE_CONFIRM: _on_force_confirm,
    EV_FORCE_ABLATE: _on_force_ablate,
    EV_FORK: _on_fork,
    EV_FORK_DONE: _on_fork_done,
    EV_INJECT_NODE: _on_inject_node,
    EV_INJECT_DONE: _on_inject_done,
    EV_DEEP_RESEARCH: _on_deep_research,
    EV_RESEARCH_COMPLETED: _on_research_completed,
    EV_LESSONS_DISTILLED: _on_lessons_distilled,
    EV_LESSONS_REFRESHED: _on_lessons_refreshed,
    EV_REPORT_GENERATED: _on_report_generated,
    EV_CONFIRM_DONE: _on_confirm_done,
    EV_ANNOTATION: _on_annotation,
    EV_COMMENT_CREATED: _on_comment,
    EV_COMMENT_EDITED: _on_comment,
    EV_COMMENT_RESOLUTION_CHANGED: _on_comment,
    EV_PROMOTE: _on_promote,
}


def fold(events: Iterable[Event]) -> RunState:
    st = RunState()
    ctx = _FoldCtx()
    for index, e in enumerate(events):
        ctx.event_index = index
        handler = _HANDLERS.get(e.type)
        # Unknown event types (e.g. "budget") are ignored for state — forward-compat.
        if handler is not None:
            handler(st, e, e.data, ctx)
    return _finalize_fold(st, ctx)


def _finalize_fold(st: RunState, ctx: _FoldCtx) -> RunState:
    """Apply the order-independent read-model tail to one isolated raw fold state."""
    # PART V (B): materialize delta-authored node concepts topologically once the whole DAG is folded
    # (order-tolerant; membership no-op unless a node authored a delta). Always invoke it so the typed
    # corruption receipt is recomputed/cleared for FoldCursor suffix snapshots as well.
    _materialize_concept_deltas(
        st,
        untrusted_modes=ctx.concept_mode_untrusted,
        capped_inputs=ctx.concept_input_capped,
        invalid_inputs=ctx.concept_input_invalid,
        base_capped=ctx.run_base_capped,
        base_invalid=ctx.run_base_invalid,
        run_base_seen=ctx.run_base_seen,
    )

    flagged = _apply_trust_gate(st)
    _select_best(st, flagged, ctx.best_confirmed, ctx.best_confirmed_significant)

    _derive_hypotheses(st)   # P1: audit-only ledger (after best is known); never touches selection
    _derive_cards(st)        # docs/23 Layer 1a: the card ledger (mirrors hypotheses); advisory, after best
    return st


class FoldCursor:
    """Incrementally accumulate an event prefix without changing ``fold`` semantics.

    Handlers mutate an *unfinalized* state in log order. ``snapshot`` deep-copies that raw state before
    applying the ordinary fold post-passes, because trust enforcement, best selection and Part-V delta
    materialization mutate their input and therefore must never leak back into the next suffix extension.
    The cursor is intentionally lock-free: its owner must serialize ``extend``/``snapshot`` as one read.
    """

    def __init__(self) -> None:
        self._state = RunState()
        self._ctx = _FoldCtx()
        self._event_count = 0

    @property
    def event_count(self) -> int:
        return self._event_count

    def extend(self, events: Iterable[Event]) -> int:
        """Apply a suffix and return the number of newly accumulated envelopes."""
        added = 0
        for e in events:
            self._ctx.event_index = self._event_count
            handler = _HANDLERS.get(e.type)
            # Unknown event types still advance the physical index because report/finish adjacency is
            # defined over envelopes, even though their state mutation is a forward-compatible no-op.
            if handler is not None:
                handler(self._state, e, e.data, self._ctx)
            self._event_count += 1
            added += 1
        return added

    def snapshot(self) -> RunState:
        """Return an independently mutable state byte-equivalent to ``fold`` of this prefix."""
        # CODEX AGENT: never finalize the accumulator itself. Several post-passes are destructive
        # (``block`` marks nodes infeasible; concept DELTAs overwrite effective memberships). A deep
        # Pydantic copy makes every GET independent and preserves the raw state for the next append.
        state = self._state.model_copy(deep=True)
        return _finalize_fold(state, self._ctx)



def _apply_trust_gate(st: RunState) -> set:
    """T2 trust enforcement post-pass: under "gate"/"block", a node flagged for a reward-hack or
    data-leakage signal must not be selectable as best (closes "a hacked/leaky node can win").
    Order-independent: computed from the folded `reward_hacks` after the full pass (see
    `flagged_node_ids`). Returns the flagged node-id set for `_select_best`."""
    flagged = flagged_node_ids(st)
    # Bar the flagged set from BREEDING/confirm targets (§2.2): under `gate` the node stays feasible
    # (kept in the tree for diversity/audit, barred only from winning) but `breedable_nodes()` skips it
    # so the search doesn't sink budget improving a cheating lineage. `block` ALSO makes it infeasible
    # (feasible=False removes it from feasible_nodes() entirely), the stricter mode.
    st.breed_excluded = set(flagged)
    if st.trust_gate == "block":
        for nid in flagged:
            nb = st.nodes.get(nid)
            if nb is not None:
                nb.feasible = False
    return flagged


def _select_best(st: RunState, flagged: set, best_confirmed: int | None,
                 best_confirmed_significant: bool = True) -> None:
    """Best-selection post-pass: derive `best_node_id` (mean-based pick -> variance-gated confirm
    override -> holdout-gated promotion) plus the audit-only generalization gap. Pure and
    deterministic over the folded state — the tail of `fold`, extracted verbatim."""
    # Multi-objective (#5): a constraint-violating node is excluded from selection — it keeps
    # its metric for the audit trail but can never be chosen best. If NOTHING is feasible,
    # there is no valid best (best_node_id stays None).
    # Exclude nodes with no usable metric: a hand-edited / BYO-script node_evaluated event can carry
    # metric=null yet fold to status=evaluated, and comparing None vs a float in the chooser below
    # would raise TypeError and brick every re-fold/resume. Such a node simply can't be "best".
    # R1/SearchFitness: the eligibility predicate, the ranked-scalar keys and the direction chooser are
    # OWNED by core.fitness.SearchFitness — one spelling shared with rank_by_metric / holdout_topk, so a
    # later scored tie-break (R1-c) composes in exactly one place. Byte-identical to the inlined logic.
    fit = SearchFitness(st.direction, verifier_tiebreak=st.select_verifier_tiebreak,
                        ci_tie=st.verifier_ci_tie)
    evaluated = promotion_eligible_nodes(st, flagged=flagged)
    if evaluated:
        # If any node has been confirmed (multi-seed), the final answer must be the
        # robust winner: rank confirmed nodes by confirmed_mean. With no confirmations
        # this is identical to ranking all evaluated nodes by their single metric.
        # R1-c: promotion_key adds a calibrated-verifier tie-break slot when select_verifier_tiebreak is
        # on — it resolves metric-EQUAL contests only, never overriding a strictly-better robust_metric.
        confirmed = [n for n in evaluated if n.confirmed_mean is not None]
        pool = confirmed if confirmed else evaluated
        # R1-d: `best_ci` widens the verifier tie-break to a STATISTICAL tie when `verifier_ci_tie` is on
        # (grounded in confirmed_std/seeds); it is IDENTICAL to the exact-tie `best(promotion_key)` when the
        # flag is off (or nodes lack confirm-noise data). §21.7: never picks over a significantly-better mean.
        st.best_node_id = fit.best_ci(pool).id

    # The variance-gated confirmation decision (I10) overrides the mean-based pick — but never
    # past the feasibility gate (#5): a constraint-violating node must not become best even if
    # the confirm phase ran on it (the mean-based pick above already excluded infeasibles).
    # The confirm certificate is the confirm phase's OWN authoritative winner (robust_selection over the
    # multi-seed means + a significance test), so it overrides the mean pick. R1-d COMPOSITION (CODEX #7):
    # the certificate overrides only when it found a SIGNIFICANT winner — OR when verifier_ci_tie is off
    # (then it overrides unconditionally, byte-identical to before). When the confirm found NO significant
    # winner (a statistical tie) AND ci_tie is on, the `best_ci` soundness pick above STANDS, because that
    # tie is EXACTLY what the CI-tie exists to resolve — an unconditional override would erase it and make
    # R1-d a no-op. Scope boundary (unchanged): among nodes the confirm DID significantly separate, the
    # winner is the confirm phase's, not the verifier's.
    if (best_confirmed is not None and best_confirmed in st.nodes
            and (best_confirmed_significant or not st.verifier_ci_tie)
            and st.nodes[best_confirmed].status is NodeStatus.evaluated
            and not st.nodes[best_confirmed].tombstoned
            and fit.eligible(st.nodes[best_confirmed], flagged, st.aborted_nodes)):
        st.best_node_id = best_confirmed

    # D1 holdout-gated promotion: when the run recorded holdout_select, the champion is the best
    # node ON THE HOLDOUT PARTITION among those that were holdout-scored (the val-top-k — so the
    # search metric still decides WHO gets a holdout eval, but the unseen signal decides who WINS).
    # Applied LAST: the holdout is a stronger discipline than the confirm mean (it is data/splits
    # the search never optimized against — AIRA: picking on the search signal overfits 9-13 pp).
    # Same guards as every other pick: feasibility + trust flags.
    if st.holdout_select and evaluated:
        hpool = [n for n in evaluated if is_usable_metric(n.holdout_metric)]
        if hpool:
            # holdout_key carries the SAME verifier tie-break slot (when select_verifier is on): a tie on
            # the unseen-signal holdout metric is broken by soundness too, so the stronger holdout signal
            # decides first and the verifier only resolves a holdout tie (never overrides it). R1-d SCOPE:
            # the holdout pick uses the EXACT-tie holdout_key, NOT the CI widening — the holdout metric is a
            # single unseen-partition score with no multi-seed std, so there is no confirm-noise CI to widen
            # with, and the unseen signal is deliberately stronger than a search-metric soundness tie-break.
            # So `verifier_ci_tie` refines only the confirmed-MEAN pick (above); when holdout_select is on
            # (default) the holdout exact-tie pick is the final word — R1-d's CI widening is effective on the
            # champion only when holdout_select is OFF.
            st.best_node_id = fit.best_holdout(hpool).id

    # An explicit human approval of a real non-best node is a selection decision, not a global latch
    # that authorizes publication of some OTHER algorithmic best. Honor it last; if the chosen node is
    # no longer eligible, invalidate the grant so the engine asks again instead of finalizing another.
    if st.approved and st.approved_node_id is not None:
        chosen = st.nodes.get(st.approved_node_id)
        if (chosen is not None and chosen.status is NodeStatus.evaluated and not chosen.tombstoned
                and fit.eligible(chosen, flagged, st.aborted_nodes)):
            st.best_node_id = chosen.id
        else:
            st.approved = False
            st.approved_node_id = None

    # Derived generalization gap (audit-only, Trust panel): how much better the search metric
    # looked than the unseen-signal metric — holdout when present, else the confirmed mean.
    # Direction-aware so positive always means "overperformed on the signal the search saw".
    for n in st.nodes.values():
        robust = n.holdout_metric if n.holdout_metric is not None else n.confirmed_mean
        if not is_usable_metric(robust) or not is_usable_metric(n.metric):
            continue
        n.generalization_gap = (n.metric - robust) if st.direction == "max" else (robust - n.metric)


def _record_setter_ids(nodes: dict[int, Node], direction: str) -> set[int]:
    """The run-global set of node ids that ADVANCED the run's SOTA — sticky evidence.

    Pure helper (Layer 1a) extracted VERBATIM from `_derive_hypotheses` so `_derive_cards` reuses the
    identical logic. A node counts if it is evaluated/feasible/non-tombstoned and, in creation order,
    either ESTABLISHES the first SOTA or BEATS the standing record; the flag STAYS set even after a
    later node overtakes it (so a draft-backed hypothesis/card does not flip supported->tested the
    moment something beats it — computing "is the CURRENT best" made it a board bug). Never mutates
    `nodes`."""
    better = (lambda a, b: a > b) if direction == "max" else (lambda a, b: a < b)
    setters: set[int] = set()
    running: float | None = None
    for n in sorted(nodes.values(), key=lambda x: x.id):
        if (n.status is NodeStatus.evaluated and n.feasible and n.metric is not None
                and not n.tombstoned):              # §6.3: a deleted node must not set the board's SOTA
            if running is None or better(n.metric, running):
                setters.add(n.id)                   # first node ESTABLISHES the SOTA, or a later node
                running = n.metric                  # BEATS the standing record — either is a real advance
    return setters


def _evidence_verdict(evidence_ids: Iterable[int], nodes: dict[int, Node], direction: str,
                      record_setters: set[int], is_abandoned: bool,
                      ) -> tuple[float | None, str, bool]:
    """Compute (best_delta, status, supported) for one hypothesis/card from its evidence nodes.

    Pure, VALUES-returning helper (Layer 1a) extracted VERBATIM from `_derive_hypotheses` so a card's
    verdict is byte-identical to the hash-joined hypothesis wherever their evidence sets coincide. NEVER
    stamps onto Node/Hypothesis/Card — it only reads. `record_setters` is `_record_setter_ids(...)`;
    `is_abandoned` is the caller's "id in <abandoned set>" check. Supported if an experiment IMPROVED
    over its parent (or set a run record); tested if evaluated without improvement; testing while
    evidence still runs; open with no (usable) evidence; abandoned overrides all."""
    better = (lambda a, b: a > b) if direction == "max" else (lambda a, b: a < b)
    ev = [nodes[i] for i in evidence_ids if i in nodes and not nodes[i].tombstoned]
    evaluated = [n for n in ev if n.status is NodeStatus.evaluated and n.feasible
                 and n.metric is not None]
    supported = False
    best_delta: float | None = None
    for n in evaluated:
        # parent metric = the best feasible-evaluated parent's metric (direction-aware)
        pmetrics = [nodes[p].metric for p in n.parent_ids
                    if p in nodes and nodes[p].metric is not None
                    and nodes[p].feasible]
        base = (max(pmetrics) if direction == "max" else min(pmetrics)) if pmetrics else None
        if base is not None:
            delta = (n.metric - base) if direction == "max" else (base - n.metric)
            best_delta = delta if best_delta is None else max(best_delta, delta)
            if better(n.metric, base):
                supported = True
        if n.id in record_setters:                 # a draft/node that advanced the run's SOTA (sticky —
            supported = True                       # stays supported even after a later node overtakes it)
    pending = [n for n in ev if n.status is NodeStatus.pending]
    if is_abandoned:
        status = "abandoned"
    elif not ev:
        status = "open"
    elif supported:
        status = "supported"                       # at least one experiment improved — verdict stands
    elif pending:
        status = "testing"                         # still inconclusive: evidence running
    elif not evaluated:
        status = "open"                            # all evidence failed/infeasible — no verdict
    else:
        status = "tested"                          # all evidence evaluated, none improved
    return best_delta, status, supported


def _derive_hypotheses(st: RunState) -> None:
    """Build the hypothesis ledger from the folded state (P1). DERIVED, not stored: every node whose
    `idea.hypothesis` is set contributes a hypothesis (id = slug of the statement) with itself as
    evidence, merged with any explicitly-added ones (`hypothesis_added`). The verdict is computed from
    evidence outcomes — supported if an experiment IMPROVED over its parent (or became the run best),
    tested if evaluated without improvement, testing while still running, open with no evidence.
    Audit-only: nothing here is read by best-selection."""
    hyps: dict[str, Hypothesis] = {}

    # 1) explicitly-added hypotheses (human / deep-research) — may start with no evidence.
    # Coerce defensively: control events arrive from the API verbatim, and one malformed entry
    # must not brick every subsequent fold of the run (same convention as node_created).
    for d in st.hypotheses_added:
        try:
            stmt = str(d.get("statement", "")).strip()
            if not stmt:
                continue
            hid = str(d.get("id") or hypothesis_id(stmt))
            if hid in hyps:
                continue
            try:
                at_node = int(d.get("at_node", 0) or 0)
            except (TypeError, ValueError):
                at_node = 0
            hyps[hid] = Hypothesis(id=hid, statement=stmt, source=str(d.get("source") or "human"),
                                   rationale=str(d.get("rationale", ""))[:400],
                                   created_at_node=at_node)
        except Exception:
            continue

    # 2) derive/merge from nodes that state a hypothesis (evidence = the node).
    for nid in sorted(st.nodes):
        n = st.nodes[nid]
        stmt = (n.idea.hypothesis or "").strip() if n.idea else ""
        if not stmt:
            continue
        hid = hypothesis_id(stmt)
        h = hyps.get(hid)
        if h is None:
            h = Hypothesis(id=hid, statement=stmt, source="researcher",
                           rationale=(n.idea.rationale or "")[:400], created_at_node=n.id)
            hyps[hid] = h
        if n.id not in h.evidence:
            h.evidence.append(n.id)

    # 2b) apply agentic merges (`hypothesis_merged` events): fold each ALIAS hypothesis's evidence into
    # its CANONICAL. Fully DETERMINISTIC — no LLM here (the decision was made + recorded by the engine);
    # order-tolerant (evidence is unioned then sorted); back-compat (no merge events -> untouched).
    alias: dict[str, str] = {}
    merged_stmt: dict[str, str] = {}
    for d in st.hypotheses_merged:
        # Per-entry guard: the dispatch only checks `aliases` is TRUTHY, so a malformed record (a
        # hand-edited log, a foreign/future writer where `aliases` is a scalar like `1`/`true`) would
        # make `for a in aliases` raise TypeError and — since `_derive_hypotheses` runs unwrapped —
        # brick EVERY subsequent fold of the run (no replay/resume/view). Tolerate it here, matching
        # the node_created / hypotheses_added handlers and the "malformed entry is tolerated" promise.
        try:
            raw_canonical = d.get("canonical")
            raw_aliases = d.get("aliases")
            if not isinstance(raw_canonical, str) or not isinstance(raw_aliases, list):
                continue
            canon = raw_canonical.strip()
            if not canon or len(canon) > 256:
                continue
            s = str(d.get("statement", "")).strip()
            if s:
                merged_stmt[canon] = s
            seen_aliases: set[str] = set()
            for raw_alias in raw_aliases[:256]:
                if not isinstance(raw_alias, str):
                    continue
                a = raw_alias.strip()
                if a and len(a) <= 256 and a != canon and a not in seen_aliases:
                    seen_aliases.add(a)
                    alias[a] = canon
        except Exception:  # noqa: BLE001 — one bad merge record must not brick the whole fold
            continue

    def _canon(x: str) -> str:                      # resolve alias chains a->b->c, cycle-safe
        seen: set[str] = set()
        while x in alias and x not in seen:
            seen.add(x)
            x = alias[x]
        return x

    control_ids: dict[str, set[str]] = {hid: {hid} for hid in hyps}
    if alias:
        folded: dict[str, Hypothesis] = {}
        folded_control_ids: dict[str, set[str]] = {}
        for hid in list(hyps):
            cid = _canon(hid)
            folded_control_ids.setdefault(cid, {cid}).update(control_ids.get(hid, {hid}))
            tgt = folded.get(cid)
            if tgt is None:
                base = hyps.get(cid, hyps[hid])     # seed from the canonical row if it exists, else this
                tgt = Hypothesis(id=cid, statement=merged_stmt.get(cid, base.statement),
                                 source=base.source, rationale=base.rationale,
                                 created_at_node=base.created_at_node)
                folded[cid] = tgt
            for e in hyps[hid].evidence:
                if e not in tgt.evidence:
                    tgt.evidence.append(e)
        for tgt in folded.values():
            tgt.evidence.sort()
        for alias_id in alias:
            canonical_id = _canon(alias_id)
            if canonical_id in folded and alias_id != canonical_id:
                folded_control_ids.setdefault(canonical_id, {canonical_id}).add(alias_id)
        hyps = folded
        control_ids = folded_control_ids

    # A node "supported" its hypothesis by ADVANCING the run's SOTA — and a record it set STAYS a support
    # even after a later node overtakes it (extracted to `_record_setter_ids`, Layer 1a, reused by
    # `_derive_cards` so a card sees the identical sticky-support set).
    _record_setters = _record_setter_ids(st.nodes, st.direction)

    # 3) compute a verdict per hypothesis from its evidence nodes — the pure `_evidence_verdict` helper
    #    (Layer 1a) returns VALUES; `_derive_cards` reuses it so a card's verdict is byte-identical to the
    #    hash-joined hypothesis wherever their evidence coincides. Never mutates the evidence nodes.
    for h in hyps.values():
        h.best_delta, h.status, _ = _evidence_verdict(
            h.evidence, st.nodes, st.direction, _record_setters,
            any(control_id in st.hypotheses_abandoned
                for control_id in control_ids.get(h.id, {h.id})))

    # FOREAGENT board prioritization: stamp each ranked card's `priority` (0-based position in the
    # latest `hypothesis_ranked` order) so the UI kanban sorts open cards by predicted payoff. Derived,
    # not stored on the event's cards — the ranking is by hypothesis id, robust to a card changing lane.
    order = (st.hypothesis_ranking or {}).get("order") or []
    for rank_i, hid in enumerate(order):
        h = hyps.get(str(hid))
        if h is not None and h.status == "open":   # priority is the OPEN lane's ordering; None once resolved
            h.priority = rank_i

    st.hypotheses = {
        hid: hypothesis for hid, hypothesis in hyps.items()
        if not any(control_id in st.hypotheses_deleted
                   for control_id in control_ids.get(hid, {hid}))
    }


def _bounded_card_enrichment(value, *, depth: int = 0, budget: list[int] | None = None):
    """Return a bounded JSON-shaped enrichment value, or ``(False, None)`` when unusable."""
    if budget is None:
        budget = [256]
    if budget[0] <= 0 or depth > 4:
        return False, None
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return (True, value) if abs(value) <= (1 << 53) - 1 else (False, None)
    if isinstance(value, float):
        return (True, value) if math.isfinite(value) else (False, None)
    if isinstance(value, str):
        return True, value[:400]
    if isinstance(value, list):
        out = []
        for item in value[:64]:
            valid, bounded = _bounded_card_enrichment(item, depth=depth + 1, budget=budget)
            if valid:
                out.append(bounded)
        return True, out
    if isinstance(value, dict):
        out = {}
        # CODEX AGENT: sorting the whole hostile map is an O(n) temporary-memory amplification before
        # the 64-row output cap. A lexical heap keeps deterministic top-K semantics in O(K) memory.
        rows = heapq.nsmallest(
            64,
            ((key, item) for key, item in value.items()
             if isinstance(key, str) and key and len(key) <= 128),
            key=lambda row: row[0],
        )
        for key, item in rows:
            valid, bounded = _bounded_card_enrichment(item, depth=depth + 1, budget=budget)
            if valid:
                out[key] = bounded
        return True, out
    return False, None


def _proposal_card_concept_source(
    kind: Literal["card_added", "card_enriched"], *, present: bool,
    overflow: bool = False, invalid: bool = False,
) -> CardConceptSource:
    reasons: set[ConceptMaterializationReason] = set()
    if overflow:
        reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
    if invalid:
        reasons.add(CONCEPT_INVALID_ID_REASON)
    receipt = concept_materialization_receipt(reasons)
    return CardConceptSource(
        kind=kind,
        membership_present=present,
        complete=present and receipt is None,
        materialization_receipt=receipt,
    )


def _card_added_snapshot(d: dict) -> tuple[dict, bool]:
    """Tolerantly decode one atomic, node-less card action snapshot."""
    idea = d.get("idea") if isinstance(d.get("idea"), dict) else d
    snapshot: dict = {}
    owns_action = False
    operator = idea.get("operator")
    if isinstance(operator, str) and operator.strip() and len(operator.strip()) <= 64:
        snapshot["operator"] = operator.strip()
        owns_action = True
    if isinstance(idea.get("params"), dict):
        snapshot["params"] = normalize_extra_metrics(idea["params"], max_items=64)
        owns_action = True
    if isinstance(idea.get("space"), dict):
        space: dict[str, list[float]] = {}
        for raw_key, raw_values in sorted(idea["space"].items(), key=lambda row: str(row[0]))[:64]:
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 200:
                continue
            if not isinstance(raw_values, list):
                continue
            values: list[float] = []
            for value in raw_values[:64]:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(number):
                    values.append(number)
            space[raw_key] = values
        snapshot["space"] = space
        owns_action = True
    profile = idea.get("eval_profile")
    if isinstance(profile, str) and len(profile) <= 256:
        snapshot["eval_profile"] = profile
        owns_action = True
    concept_key_present = (
        "concept_tags" in idea or "concepts" in idea
        or "_concept_tags_overflow" in idea or "_concept_tags_invalid" in idea
    )
    raw_concepts = idea.get("concept_tags", idea.get("concepts"))
    if concept_key_present:
        values, overflow, invalid = bounded_raw_concept_values(raw_concepts)
        # Sanitized receipts carry these internal flags because the compact list alone cannot prove
        # whether the original membership was complete. Payload-provided flag fields are not copied.
        overflow = overflow or idea.get("_concept_tags_overflow") is True
        invalid = invalid or idea.get("_concept_tags_invalid") is True
        snapshot["concept_source"] = _proposal_card_concept_source(
            "card_added", present=isinstance(raw_concepts, list), overflow=overflow, invalid=invalid)
    else:
        snapshot["concept_source"] = _proposal_card_concept_source(
            "card_added", present=False)
    if isinstance(raw_concepts, list):
        snapshot["concept_tags"] = values
        owns_action = True

    raw_parent_ids = d.get("parent_ids", idea.get("parent_ids"))
    if isinstance(raw_parent_ids, list):
        parent_ids: list[int] = []
        for raw in raw_parent_ids[:64]:
            nid = _coerce_node_id({"node_id": raw})
            if nid is not None and 0 <= nid <= (1 << 31) - 1 and nid not in parent_ids:
                parent_ids.append(nid)
        snapshot["parent_ids"] = parent_ids
        owns_action = True
    raw_parent_id = d.get("parent_id", idea.get("parent_id"))
    parent_id = _coerce_node_id({"node_id": raw_parent_id})
    if parent_id is not None and 0 <= parent_id <= (1 << 31) - 1:
        snapshot["parent_id"] = parent_id
        owns_action = True
    elif snapshot.get("parent_ids"):
        snapshot["parent_id"] = snapshot["parent_ids"][0]

    scored_against = _coerce_node_id({"node_id": d.get("scored_against")})
    if scored_against is not None and 0 <= scored_against <= (1 << 31) - 1:
        snapshot["scored_against"] = scored_against
    if isinstance(d.get("footprint"), dict):
        footprint = normalize_researcher_footprint(d["footprint"])
        if footprint is not None:
            snapshot["footprint"] = footprint
    if isinstance(d.get("steering_context"), list):
        steering: list[dict] = []
        for item in d["steering_context"][:64]:
            if not isinstance(item, dict):
                continue
            valid, bounded = _bounded_card_enrichment(item)
            if valid and isinstance(bounded, dict):
                steering.append(bounded)
        snapshot["steering_context"] = steering
    return snapshot, owns_action


_CARD_ADDED_ACTION_FIELDS = frozenset({
    "operator", "params", "space", "eval_profile", "concept_tags", "concepts",
    "parent_id", "parent_ids", "_concept_tags_overflow", "_concept_tags_invalid",
})


def _card_action_receipt_payload(snapshot: dict) -> dict:
    """Extract exactly the immutable action subset covered by the v1 ownership digest."""
    return {field: snapshot.get(field) for field in CARD_ACTION_DIGEST_V1_FIELDS}


def _card_added_ownership(
    d: dict, card_id: str, statement: str, snapshot: dict, *, owns_action: bool,
) -> tuple[bool, bool, str | None]:
    """Validate a native identity receipt and whether its action was losslessly represented."""
    explicit_id = d.get("id")
    expected = card_ownership_receipt(card_id, statement, _card_action_receipt_payload(snapshot))
    receipt_valid = bool(
        isinstance(explicit_id, str)
        and explicit_id == card_id
        and expected is not None
        and d.get("ownership_receipt") == expected
    )
    raw_idea = d.get("idea")
    if not receipt_valid or not owns_action or not isinstance(raw_idea, dict):
        return receipt_valid, False, expected.get("action_digest") if expected else None

    # CODEX AGENT: the receipt covers the complete executable subset. Unknown Idea members may gain
    # execution meaning in a later schema, so an old reader cannot silently discard them and still call
    # the action complete. Concept membership is the sole exception: it is metadata with its own receipt.
    if not set(raw_idea) <= _CARD_ADDED_ACTION_FIELDS:
        return True, False, expected["action_digest"]
    if d.get("footprint") is not None and not valid_researcher_footprint(d.get("footprint")):
        return True, False, expected["action_digest"]
    raw_action = {
        "operator": raw_idea.get("operator"),
        "params": raw_idea.get("params"),
        "space": raw_idea.get("space"),
        "eval_profile": raw_idea.get("eval_profile"),
        "parent_id": d.get("parent_id", raw_idea.get("parent_id")),
        "parent_ids": d.get("parent_ids", raw_idea.get("parent_ids", [])),
        "scored_against": d.get("scored_against"),
        "footprint": d.get("footprint"),
    }
    raw_expected = card_ownership_receipt(card_id, statement, raw_action)
    action_complete = raw_expected == expected
    return True, action_complete, expected["action_digest"]


def _card_action_from_projection(card: Card) -> dict:
    return {
        "operator": card.operator,
        "params": card.params,
        "space": card.space,
        "eval_profile": card.eval_profile,
        "parent_id": card.parent_id,
        "parent_ids": card.parent_ids,
        "scored_against": card.scored_against,
        "footprint": card.footprint,
    }


def _card_action_has_live_anchors(card: Card, breedable_node_ids: set[int]) -> bool:
    """Whether the bounded action has one executable operator/parent shape right now."""
    operator = card.operator
    parent_ids = list(card.parent_ids or [])
    if card.parent_id is not None:
        if parent_ids and parent_ids[0] != card.parent_id:
            return False
        if not parent_ids:
            parent_ids = [card.parent_id]
    if len(parent_ids) != len(set(parent_ids)):
        return False
    if operator == "draft":
        return not parent_ids
    if operator in {"improve", "debug"}:
        expected_parents = 1
    elif operator == "merge":
        expected_parents = 2
    else:
        return False
    if len(parent_ids) != expected_parents:
        return False
    return all(parent_id in breedable_node_ids for parent_id in parent_ids)


def _card_sidecar_subject(st: RunState, d: dict, node_to_card: dict[int, str], *,
                          legacy_reproposed_nodes: set[int] | None = None,
                          cross_run: bool = False) -> str | None:
    """Resolve one sidecar only when its exact proposal/lifecycle subject still owns the node."""
    raw_node_id = d.get("node_id")
    if type(raw_node_id) is not int or raw_node_id < 0:
        return None
    node = st.nodes.get(raw_node_id)
    card_id = node_to_card.get(raw_node_id)
    if node is None or card_id is None or node.idea is None:
        return None
    if node.tombstoned or node.id in st.aborted_nodes:
        return None

    has_ref = "proposal_ref" in d
    has_generation = "generation" in d
    if not has_ref and not has_generation:
        # Historical rows predate exact bindings. Preserve only their original generation-0 behavior;
        # a reproposed rejection explicitly refers to the discarded proposal, and its sibling legacy
        # cross-run row is equally ambiguous for the replacement occupying that slot.
        if node.attempt != 0 or d.get("action") == "reproposed":
            return None
        if cross_run and legacy_reproposed_nodes and raw_node_id in legacy_reproposed_nodes:
            return None
        return card_id

    ref = d.get("proposal_ref")
    generation = d.get("generation")
    if (type(generation) is not int or generation < 0 or generation != node.attempt
            or not isinstance(ref, dict) or set(ref) != {"v", "digest"}
            or ref.get("v") != 1 or not isinstance(ref.get("digest"), str)):
        return None
    expected = idea_proposal_digest(node.idea)
    if expected is None or ref["digest"] != expected:
        return None
    # A modern `reproposed` rejection is deliberately bound to the discarded original. Even if a buggy
    # writer duplicated the digest, never annotate the replacement card with that negative verdict.
    if d.get("action") == "reproposed":
        return None
    return card_id


def _card_novelty_projection(st: RunState, d: dict) -> dict:
    def _text(key: str) -> str | None:
        value = d.get(key)
        return value[:200] if isinstance(value, str) else None

    near_node = d.get("near_node")
    if type(near_node) is not int or near_node < 0:
        near_node = None
    projection = {
        "grade": _text("grade"),
        "level": d.get("level") if type(d.get("level")) is int and 0 <= d["level"] <= 16 else None,
        "near_node": near_node,
        "recommendation": _text("recommendation"),
    }
    near_generation = d.get("near_generation")
    if type(near_generation) is int and near_generation >= 0:
        projection["near_generation"] = near_generation
    if "proposal_ref" in d or "generation" in d:
        # CODEX AGENT: a modern near-node reference names a lifecycle, not a reusable numeric slot.
        # Never let a reset, tombstone, abort, absent row, or malformed generation re-home the verdict
        # onto whichever proposal happens to occupy that id at the end of replay.
        near = st.nodes.get(near_node) if near_node is not None else None
        if (near is None or type(near_generation) is not int or near_generation < 0
                or near.attempt != near_generation or near.tombstoned
                or near.id in st.aborted_nodes):
            projection["near_node"] = None
    return projection


def _card_cross_run_projection(d: dict) -> dict:
    matched = []
    for item in (d.get("matched_concepts") if isinstance(d.get("matched_concepts"), list) else [])[:64]:
        if isinstance(item, str) and item and len(item) <= 256 and item not in matched:
            matched.append(item)
    raw_runs = d.get("prior_runs") if isinstance(d.get("prior_runs"), list) else []
    prior_runs = []
    runs_lossy = len(raw_runs) > 64
    for item in raw_runs[:64]:
        if not isinstance(item, dict):
            runs_lossy = True
            continue
        valid, bounded = _bounded_card_enrichment(item)
        if valid and isinstance(bounded, dict):
            prior_runs.append(bounded)
            runs_lossy = runs_lossy or bounded != item
        else:
            runs_lossy = True

    def _count(key):
        value = d.get(key)
        return value if type(value) is int and 0 <= value <= (1 << 53) - 1 else None

    total = _count("prior_runs_total")
    omitted = _count("prior_runs_omitted")
    projection_drops = max(0, len(raw_runs) - len(prior_runs))
    if total is not None:
        # A bounded card can retain fewer rows than the durable receipt. Never project the producer's
        # pre-projection zero as if this now-truncated view were exact.
        omitted = max(omitted or 0, max(0, total - len(prior_runs)))
    elif omitted is not None and projection_drops:
        omitted = min((1 << 53) - 1, omitted + projection_drops)
    declared_complete = d.get("prior_runs_complete") is True
    complete = bool(
        declared_complete and not runs_lossy and len(prior_runs) == len(raw_runs)
        and total == len(prior_runs) and omitted == 0
    )
    raw_source = d.get("concept_source")
    valid, concept_source = _bounded_card_enrichment(raw_source) if isinstance(raw_source, dict) else (False, {})
    if not valid or not isinstance(concept_source, dict):
        concept_source = {}
    # Completeness is affirmative evidence. Any malformed/truncated source receipt becomes explicitly
    # partial rather than looking exact merely because the retained runs are well formed.
    if (not isinstance(raw_source, dict) or len(raw_source) > 64 or concept_source != raw_source
            or raw_source.get("source_complete") is not True):
        concept_source["source_complete"] = False
    else:
        concept_source["source_complete"] = True
    return {
        "v": d.get("v") if type(d.get("v")) is int else None,
        "matched_concepts": matched,
        "prior_runs": prior_runs,
        "prior_runs_total": total,
        "prior_runs_omitted": omitted,
        "prior_runs_complete": complete,
        "concept_source": concept_source,
    }


_CARD_NODE_CONCEPT_PROVENANCE = frozenset({
    NODE_CONCEPT_PROVENANCE_AUTHORED,
    NODE_CONCEPT_PROVENANCE_CLASSIFIER,
    NODE_CONCEPT_PROVENANCE_OPERATOR,
    NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
    NODE_CONCEPT_PROVENANCE_UNTRUSTED,
})


def _card_node_concept_projection(st: RunState, node: Node) -> tuple[list[str], CardConceptSource]:
    """Project one exact node owner from the already-finalized concept read model."""
    memberships = getattr(st, "node_concepts", None)
    membership_map_valid = isinstance(memberships, dict)
    membership_present = membership_map_valid and node.id in memberships
    raw_membership = memberships.get(node.id) if membership_present else []
    tags, overflow, invalid = bounded_raw_concept_values(raw_membership)

    receipts = getattr(st, "node_concept_materialization_receipts", None)
    receipt_map_valid = isinstance(receipts, dict)
    raw_receipt = receipts.get(node.id, _MISSING) if receipt_map_valid else None
    receipt = (
        normalized_concept_materialization_receipt(raw_receipt)
        if raw_receipt is not _MISSING else None
    )
    receipt_valid = receipt_map_valid and (
        raw_receipt is _MISSING or receipt is not None)
    reasons: set[ConceptMaterializationReason] = set(
        receipt["reasons"] if receipt is not None else ())
    if overflow:
        reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
    if invalid or (membership_present and not isinstance(raw_membership, list)):
        reasons.add(CONCEPT_INVALID_ID_REASON)
    materialization_receipt = concept_materialization_receipt(reasons)

    provenance_map = getattr(st, "node_concept_provenance", None)
    raw_provenance = provenance_map.get(node.id) if isinstance(provenance_map, dict) else None
    provenance_known = raw_provenance in _CARD_NODE_CONCEPT_PROVENANCE
    provenance = (
        raw_provenance if provenance_known else
        NODE_CONCEPT_PROVENANCE_UNTRUSTED if raw_provenance is not None else None
    )
    # CODEX AGENT: `[]` with membership_present=True and no receipt is an exact empty set.  The same
    # value with an absent key, a corrupt receipt, or an unavailable delta is explicitly incomplete.
    source = CardConceptSource(
        kind="node",
        node_id=node.id,
        node_generation=node.attempt,
        provenance=provenance,
        membership_present=membership_present,
        complete=(membership_present and membership_map_valid and provenance_known
                  and receipt_valid and materialization_receipt is None),
        receipt_valid=receipt_valid,
        materialization_receipt=materialization_receipt,
    )
    return tags, source


def _derive_cards(st: RunState) -> None:
    """Build the CARD ledger (Kanban re-architecture, docs/23 Layer 1a). DERIVED, order-tolerant,
    ADVISORY — never read by best-selection, exactly like `_derive_hypotheses`, which it MIRRORS. A card
    is the rich superset of a hypothesis: seeded from `card_added` events and from every node whose
    `idea.hypothesis` is set (linked by `idea.card_id` when present, else the statement hash — the same
    fallback join `_derive_hypotheses` uses), it accretes the substance on Idea/Node. The `verdict`
    (supported/tested/...) is computed by the SHARED `_evidence_verdict` helper so it is byte-identical
    to the hash-joined hypothesis; a separate lifecycle `status` lane (proposed/running/gated/evaluated/
    dropped) is derived from node outcomes. Enrichment fields stay at their defaults in Layer 1a (Layer
    1b populates them). Internal order MIRRORS `_derive_hypotheses` EXACTLY (docs/23 decision 20):
    seed+link -> merge-union -> record-setters once -> shared verdict helper -> drop overrides ->
    operator-overlay (reserved, empty in L1) -> status."""
    cards: dict[str, Card] = {}

    # A legacy hypothesis shadow uses hypothesis_id(statement), while a staged card has an independent
    # stable id. Bridge the hash only when there is exactly one native id for that seed. Two different
    # native ids may carry different action blocks; guessing an owner would silently lose work, so the
    # ambiguous hash row stays audit-only and neither native card inherits hash-addressed controls.
    native_ids_by_statement: dict[str, list[str]] = {}
    statements_by_native_id: dict[str, list[str]] = {}
    statements_by_seed_hash: dict[str, list[str]] = {}
    seed_hash_by_statement: dict[str, str] = {}

    def _card_id(value) -> str | None:
        if not isinstance(value, str):
            return None
        bounded = value.strip()
        return bounded if bounded and len(bounded) <= 256 and bounded.isprintable() else None

    def _register_card_identity(statement: str, raw_native_id=None) -> None:
        if not statement:
            return
        seed_id = hypothesis_id(statement)
        statement_id = hypothesis_statement_digest(statement)
        seed_hash_by_statement[statement_id] = seed_id
        seed_statements = statements_by_seed_hash.setdefault(seed_id, [])
        if statement_id not in seed_statements:
            seed_statements.append(statement_id)
        cid = _card_id(raw_native_id)
        if cid is None or cid == seed_id:
            return
        native_ids = native_ids_by_statement.setdefault(statement_id, [])
        if cid not in native_ids:
            native_ids.append(cid)
        native_statements = statements_by_native_id.setdefault(cid, [])
        if statement_id not in native_statements:
            native_statements.append(statement_id)

    # CODEX AGENT: identity conflicts can enter through a staged card, a legacy hypothesis, or a node
    # whose Idea already carries card_id. Scan all three durable sources before creating any card; only
    # scanning card_added would let node-only logs reuse one stable id and silently conflate evidence.
    for d in st.cards_added:
        try:
            statement = str(d.get("statement") or "").strip()
            _register_card_identity(statement, d.get("id"))
        except Exception:  # noqa: BLE001 - malformed staging rows remain audit-only
            continue
    for d in st.hypotheses_added:
        try:
            _register_card_identity(str(d.get("statement") or "").strip())
        except Exception:  # noqa: BLE001 - malformed legacy rows remain audit-only
            continue
    for node in st.nodes.values():
        try:
            statement = str(node.idea.hypothesis or "").strip() if node.idea is not None else ""
            _register_card_identity(
                statement, node.idea.card_id if node.idea is not None else None)
        except Exception:  # noqa: BLE001 - malformed historical nodes remain independently visible
            continue
    namespace_conflicts = {
        cid for cid, statement_ids in statements_by_native_id.items()
        if any(target not in statement_ids for target in statements_by_seed_hash.get(cid, ()))
    }
    # Reusing one explicit id for two full statements is unrepresentable and must be suppressed. A
    # different case is an explicit id that merely happens to equal another statement's legacy short
    # hash: both explicit cards can still be preserved, but that shared spelling is unsafe for controls.
    conflicted_native_ids = {
        cid for cid, statement_ids in statements_by_native_id.items() if len(statement_ids) > 1
    }
    ambiguous_statement_ids = {
        statement_id for statement_id, native_ids in native_ids_by_statement.items()
        if (len(native_ids) != 1 or native_ids[0] in conflicted_native_ids
            or seed_hash_by_statement[statement_id] in namespace_conflicts
            or len(statements_by_seed_hash.get(seed_hash_by_statement[statement_id], ())) != 1)
    }
    owner_by_statement = {
        statement_id: native_ids[0]
        for statement_id, native_ids in native_ids_by_statement.items()
        if statement_id not in ambiguous_statement_ids
    }
    seed_owner = {
        seed_hash_by_statement[statement_id]: owner
        for statement_id, owner in owner_by_statement.items()
    }
    ambiguous_seeds = {
        seed_hash_by_statement[statement_id] for statement_id in ambiguous_statement_ids
    } | {
        seed_id for seed_id, statement_ids in statements_by_seed_hash.items() if len(statement_ids) > 1
    } | namespace_conflicts
    action_owned_cards: set[str] = set()
    card_origins: dict[str, str] = {}
    card_registrations: dict[str, dict] = {}
    action_owners: dict[str, dict] = {}

    def _record_registration(card_id: str, *, valid: bool, digest: str | None) -> None:
        row = card_registrations.setdefault(
            card_id, {"count": 0, "valid_count": 0, "digest": None})
        row["count"] = min(257, row["count"] + 1)
        if valid:
            row["valid_count"] = min(257, row["valid_count"] + 1)
            row["digest"] = digest if row["valid_count"] == 1 else None

    def _record_action_owner(card_id: str, source: str, *, complete: bool) -> None:
        row = action_owners.setdefault(
            card_id, {"count": 0, "sources": set(), "all_complete": True})
        row["count"] = min(257, row["count"] + 1)
        row["sources"].add(source)
        row["all_complete"] = row["all_complete"] and complete

    # 1) explicitly-added cards — may start with no evidence. Coerce defensively (engine/control events
    #    arrive verbatim; one malformed entry must not brick every fold). Until the engine mints `card_*`
    #    events (a later increment), Layer 1a MIRRORS `_derive_hypotheses` by ALSO seeding from the
    #    engine-populated `hypotheses_added` (deep-research/human directions), so a node-less hypothesis
    #    still becomes a card and `st.cards` stays a faithful shadow of `st.hypotheses`. `card_*` first so
    #    a real card_added (explicit id/source) wins the id over its hypothesis twin (dedup = first wins).
    for native_row, d in (
            [(True, row) for row in st.cards_added]
            + [(False, row) for row in st.hypotheses_added]):
        try:
            stmt = str(d.get("statement", "")).strip()
            seed_id = hypothesis_id(stmt) if stmt else ""
            statement_id = hypothesis_statement_digest(stmt) if stmt else ""
            raw_id = d.get("id")
            raw_cid = _card_id(raw_id) or seed_id
            if raw_cid in conflicted_native_ids:
                continue
            # CODEX AGENT: never materialize a third, hash-addressed queue item beside ambiguous native
            # cards. The raw hypothesis/card event remains the durable audit receipt.
            if seed_id in ambiguous_seeds and (not native_row or raw_cid == seed_id):
                continue
            cid = owner_by_statement.get(statement_id, raw_cid) if raw_cid == seed_id else raw_cid
            if not cid or len(cid) > 256:
                continue
            try:
                at_node = int(d.get("at_node", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                at_node = 0
            if not 0 <= at_node <= (1 << 31) - 1:
                at_node = 0
            snapshot, owns_action = _card_added_snapshot(d) if native_row else ({}, False)
            receipt_valid, action_complete, action_digest = (
                _card_added_ownership(d, cid, stmt, snapshot, owns_action=owns_action)
                if native_row else (False, False, None)
            )
            if native_row:
                _record_registration(cid, valid=receipt_valid, digest=action_digest)
                if owns_action:
                    _record_action_owner(cid, "card_added", complete=action_complete)
            if cid in cards:
                continue
            cards[cid] = Card(
                id=cid, statement=stmt, seed_statement=stmt,
                source=str(d.get("source") or "human"),   # mirror _derive_hypotheses' default
                rationale=str(d.get("rationale", ""))[:400], created_at_node=at_node,
                **snapshot,
            )
            card_origins[cid] = "card_added_unbound" if native_row else "hypothesis_shadow"
            if owns_action:
                action_owned_cards.add(cid)
        except Exception:  # noqa: BLE001 — one bad record must not brick the fold
            continue

    # 2) derive/link from nodes that state a hypothesis (evidence = the node). Link by `idea.card_id`
    #    (Layer-1a stable id) when present, else the statement hash (legacy/derived fallback), mirroring
    #    `_derive_hypotheses`. `sorted(st.nodes)` keeps evidence order == the hypothesis shadow's.
    for nid in sorted(st.nodes):
        n = st.nodes[nid]
        if n.idea is None:
            continue
        stmt = (n.idea.hypothesis or "").strip()
        seed_id = hypothesis_id(stmt) if stmt else ""
        statement_id = hypothesis_statement_digest(stmt) if stmt else ""
        explicit_card_id = (n.idea.card_id or "").strip()
        raw_cid = explicit_card_id or seed_id
        if explicit_card_id in conflicted_native_ids:
            continue
        if not explicit_card_id and seed_id in ambiguous_seeds:
            continue  # no exact native identity: attaching legacy evidence would be an arbitrary guess
        cid = owner_by_statement.get(statement_id, raw_cid) if raw_cid == seed_id else raw_cid
        if not cid:
            continue
        existing_action = action_owners.get(cid)
        if existing_action is None or "card_added" not in existing_action["sources"]:
            _record_action_owner(cid, "node", complete=False)
        node_concept_tags, node_concept_source = _card_node_concept_projection(st, n)
        c = cards.get(cid)
        if c is None:
            c = Card(id=cid, statement=stmt, seed_statement=stmt, source="researcher",
                     rationale=(n.idea.rationale or "")[:400], created_at_node=n.id,
                     operator=n.idea.operator, params=dict(n.idea.params or {}),
                     space={k: list(v) for k, v in (n.idea.space or {}).items()},
                     eval_profile=n.idea.eval_profile, concept_tags=node_concept_tags,
                     concept_source=node_concept_source,
                     provenance_tier=node_concept_source.provenance,
                     parent_id=(n.parent_ids[0] if n.parent_ids else None),
                     parent_ids=list(n.parent_ids or []))
            cards[cid] = c
            card_origins[cid] = "node_card_id" if explicit_card_id else "node_statement_hash"
            action_owned_cards.add(cid)
        elif not c.evidence and cid not in action_owned_cards:
            # CODEX AGENT: card_added is intentionally thin. Backfill its missing action block from the
            # earliest linked node; otherwise the normal card_added -> node_created staging path leaves a
            # permanently substance-free card. Copy the whole block atomically (including legitimate
            # empties) so later evidence cannot synthesize a chimera from several proposals.
            c.operator = n.idea.operator
            c.params = dict(n.idea.params or {})
            c.space = {k: list(v) for k, v in (n.idea.space or {}).items()}
            c.eval_profile = n.idea.eval_profile
            c.parent_id = n.parent_ids[0] if n.parent_ids else None
            c.parent_ids = list(n.parent_ids or [])
            action_owned_cards.add(cid)
        if c.concept_source is None or c.concept_source.kind != "node":
            # CODEX AGENT: the first linked node is the exact action/evidence owner.  Later evidence may
            # have classifier/operator tags of its own, but folding those into one card would create a
            # provenance lie.  Node ids are visited in sorted order, so ownership is replay-order stable.
            c.concept_tags = node_concept_tags
            c.concept_source = node_concept_source
            c.provenance_tier = node_concept_source.provenance
        if n.id not in c.evidence:
            c.evidence.append(n.id)

    # 2b) apply `card_merged` events (fold each ALIAS card's evidence into its CANONICAL) — fully
    #     DETERMINISTIC (no LLM; the decision was recorded by the engine), order-tolerant, cycle-safe.
    #     Mirrors `_derive_hypotheses` 2b exactly, reusing the same `_canon` alias-chain resolution.
    alias: dict[str, str] = {}
    for seed_id, owner in seed_owner.items():
        if seed_id != owner:
            alias[seed_id] = owner
    merged_stmt: dict[str, str] = {}
    for native_merge, d in (
            [(True, row) for row in st.cards_merged]
            + [(False, row) for row in st.hypotheses_merged]):
        try:
            raw_canonical = d.get("canonical")
            raw_aliases = d.get("aliases")
            if not isinstance(raw_aliases, list):
                continue
            raw_canon = _card_id(raw_canonical)
            if raw_canon is None or (not native_merge and raw_canon in ambiguous_seeds):
                continue
            canon = seed_owner.get(raw_canon, raw_canon)
            s = str(d.get("statement", "")).strip()
            if s:
                merged_stmt[canon] = s
            seen_aliases: set[str] = set()
            for raw_alias in raw_aliases[:256]:
                a = _card_id(raw_alias)
                if a is None or (not native_merge and a in ambiguous_seeds):
                    continue
                resolved_alias = seed_owner.get(a, a)
                if resolved_alias != canon and resolved_alias not in seen_aliases:
                    seen_aliases.add(resolved_alias)
                    # Preserve hash -> native ownership and compose native -> canonical. Overwriting the
                    # first edge would strand the stable card beside its merged hypothesis shadow.
                    alias[resolved_alias] = canon
        except Exception:  # noqa: BLE001 — one bad merge record must not brick the fold
            continue

    def _canon(x: str) -> str:                      # resolve alias chains a->b->c, cycle-safe
        seen: set[str] = set()
        while x in alias and x not in seen:
            seen.add(x)
            x = alias[x]
        return x

    # Legacy hypothesis controls name a statement hash while a modern card may have a stable independent
    # id. Carry every pre-merge id and seed hash forward to the final canonical card.
    control_ids: dict[str, set[str]] = {}
    for cid, c in cards.items():
        ids = {cid}
        if c.seed_statement:
            ids.add(hypothesis_id(c.seed_statement))
        # A spelling shared by a native id and another statement's legacy hash cannot identify which
        # card an old control intended. Preserve both cards, but apply no ambiguous control by guessing.
        control_ids[cid] = {control_id for control_id in ids if control_id not in ambiguous_seeds}

    if alias:
        folded: dict[str, Card] = {}
        folded_control_ids: dict[str, set[str]] = {}
        folded_origins: dict[str, str] = {}
        folded_action_owners: dict[str, dict] = {}
        grouped: dict[str, list[str]] = {}
        for cid in sorted(cards):
            grouped.setdefault(_canon(cid), []).append(cid)
        for tid in sorted(grouped):
            members = grouped[tid]
            # CODEX AGENT: if a merge names no materialized canonical row, event insertion order must not
            # choose the surviving action/concept owner. Prefer a canonical action; otherwise choose the
            # lexically first concrete action and copy its WHOLE block plus concept receipt together.
            action_candidates = [cid for cid in members if cid in action_owned_cards]
            action_owner_id = (
                tid if tid in action_owned_cards else
                action_candidates[0] if action_candidates else
                tid if tid in cards else members[0]
            )
            base_id = tid if tid in cards else action_owner_id
            tgt = cards[base_id].model_copy(deep=True)
            if action_owner_id != base_id:
                action_owner = cards[action_owner_id].model_copy(deep=True)
                for field in (
                    "operator", "params", "space", "eval_profile", "concept_tags", "concept_source",
                    "provenance_tier", "parent_id", "parent_ids", "scored_against",
                ):
                    setattr(tgt, field, getattr(action_owner, field))
            tgt.id = tid
            if tid in merged_stmt:
                tgt.statement = merged_stmt[tid]    # DISPLAY statement; seed remains the join key
            tgt.evidence = sorted({
                evidence for cid in members for evidence in cards[cid].evidence
            })
            tgt.aliases = sorted({
                alias_id for cid in members
                for alias_id in ([cid] if cid != tid else []) + list(cards[cid].aliases)
                if alias_id != tid
            })
            folded[tid] = tgt
            folded_origins[tid] = card_origins.get(tid, "merge")
            owner_rows = [action_owners[cid] for cid in members if cid in action_owners]
            if owner_rows:
                folded_action_owners[tid] = {
                    "count": min(257, sum(row["count"] for row in owner_rows)),
                    "sources": set().union(*(row["sources"] for row in owner_rows)),
                    "all_complete": all(row["all_complete"] for row in owner_rows),
                }
            target_controls = folded_control_ids.setdefault(
                tid, {tid} if tid not in ambiguous_seeds else set())
            for cid in members:
                target_controls.update(control_ids.get(cid, set()))
        for alias_id in alias:
            target_id = _canon(alias_id)
            target = folded.get(target_id)
            if target is not None and alias_id != target_id and alias_id not in target.aliases:
                target.aliases.append(alias_id)
                target.aliases.sort()
                target_controls = folded_control_ids.setdefault(
                    target_id, {target_id} if target_id not in ambiguous_seeds else set(),
                )
                if alias_id not in ambiguous_seeds:
                    target_controls.add(alias_id)
        cards = folded
        control_ids = folded_control_ids
        card_origins = folded_origins
        action_owners = folded_action_owners

    # 3) record-setters (sticky SOTA advancers) — the SAME pure helper the hypotheses use, so a card's
    #    verdict is byte-identical to its hash-joined hypothesis.
    _record_setters = _record_setter_ids(st.nodes, st.direction)

    # 4) verdict per card via the SHARED helper (open/testing/supported/tested/abandoned). `is_abandoned`
    #    mirrors the hypothesis: a shadow card keyed by the hypothesis id inherits the abandoned override.
    for c in cards.values():
        c.best_delta, c.verdict, _ = _evidence_verdict(
            c.evidence, st.nodes, st.direction, _record_setters,
            any(control_id in st.hypotheses_abandoned
                for control_id in control_ids.get(c.id, {c.id})))

    # 5) apply `card_dropped` overrides (engine/operator). The card STAYS visible (like an abandoned
    #    hypothesis) — the lifecycle `status` below reads this to show the 'dropped' lane. Last write wins.
    dropped: dict[str, dict] = {}
    for d in st.cards_dropped:
        raw_id = d.get("id")
        bounded_id = _card_id(raw_id)
        if bounded_id is None:
            continue
        cid = _canon(bounded_id)
        if cid:
            dropped[cid] = d
    for cid, d in dropped.items():
        c = cards.get(cid)
        if c is not None:
            reason = str(d.get("reason", "") or "")[:400]
            c.dropped_reason = reason or None
            c.dropped_by = str(d.get("dropped_by") or d.get("by") or "engine")

    # 6) lifecycle `status` lane (frozen vocab; DISTINCT from the verdict). Dropped/merged-away wins;
    #    else a pending node -> running; else evidence all trust-gated/breed-excluded/infeasible -> gated;
    #    else terminal evidence -> evaluated; no evidence -> proposed. (building/coded lanes need the
    #    node_building.card_id link minted in a later increment — not populated in Layer 1a.)
    for cid, c in cards.items():
        if cid in dropped or c.merged_into:
            c.status = "dropped"
            continue
        ev_nodes = [st.nodes[i] for i in c.evidence if i in st.nodes and not st.nodes[i].tombstoned]
        if not ev_nodes:
            c.status = "proposed"
        elif any(n.status is NodeStatus.pending for n in ev_nodes):
            c.status = "running"
        elif all((n.id in st.breed_excluded) or (not n.feasible) for n in ev_nodes):
            c.status = "gated"
        else:
            c.status = "evaluated"

    # 6b) LAYER-1b ENRICHMENT — re-home the folded "homeless" signals onto the card + apply explicit
    #     card_enriched deltas. Every source is ALREADY folded (the linking node's Idea, the novelty/
    #     cross-run sidecars) or a main-task card event, so this stays pure/deterministic. Operator
    #     overrides (step 7) run AFTER, so an operator pin always wins over an engine enrichment.
    node_to_card: dict[int, str] = {}
    for cid, c in cards.items():
        for nid in c.evidence:
            node_to_card.setdefault(nid, cid)   # first card claiming a node wins (evidence is per-card)

    # Researcher-proposed footprint + research origin ride the linking node's Idea/Node (earliest wins).
    for c in cards.values():
        for nid in c.evidence:
            n = st.nodes.get(nid)
            if n is None or n.idea is None:
                continue
            if c.footprint is None and n.idea.footprint:
                c.footprint = {**n.idea.footprint, "proposed_by": "researcher"}
            if c.research_origin is None and isinstance(n.research_origin, dict):
                # Node.research_origin is the deep-research provenance {at_node, trigger} (orchestrator
                # stamps it; models.py:530). Ref-shaped by the node it was triggered at (docs/23 dec 23).
                _at = n.research_origin.get("at_node")
                if _at is not None:
                    c.research_origin = f"node:{_at}"
            if c.footprint is not None and c.research_origin is not None:
                break

    # Novelty verdict + cross-run prior — the sidecar signals, keyed by the (prospective -> actual) node
    # id they were emitted for. Last write per node wins; ref-shaped (no verbatim capture on the card).
    # `novelty_events` (near-duplicate rejects — no grade) go FIRST so a richer `novelty_grades` entry
    # for the same node wins on collision instead of being clobbered by the sparse reject.
    legacy_reproposed_nodes = {
        d["node_id"] for d in st.novelty_events
        if type(d.get("node_id")) is int and d.get("action") == "reproposed"
        and "proposal_ref" not in d and "generation" not in d
    }
    for d in list(st.novelty_events) + list(st.novelty_grades):
        cid = _card_sidecar_subject(st, d, node_to_card)
        if cid:
            cards[cid].novelty_verdict = _card_novelty_projection(st, d)
    for d in st.cross_run_priors:
        cid = _card_sidecar_subject(
            st, d, node_to_card, legacy_reproposed_nodes=legacy_reproposed_nodes, cross_run=True)
        if cid:
            cards[cid].cross_run_prior = _card_cross_run_projection(d)

    # Explicit card_enriched deltas — last-write-by-seq. An ALLOW-LIST is the ONLY thing that protects the
    # shadow: a field NOT listed here (id/statement/verdict/status/evidence/best_delta/...) is never
    # touched, so a malformed/hostile delta cannot overwrite a shadow-load-bearing field. Each field is
    # type-guarded and the two numeric coercions are guarded INDIVIDUALLY, so a bad numeric field can
    # never drop a valid sibling field that appears after it in the delta (key-order-independent apply).
    _ENRICH_DICT = {"novelty_verdict", "cross_run_prior", "footprint"}
    _ENRICH_REFS = {"lesson_refs", "claim_refs"}
    _ENRICH_STR = {"research_origin"}
    for d in sorted(st.cards_enriched, key=lambda r: (
            r.get("_seq") if type(r.get("_seq")) is int else -1,
            r.get("_event_index") if type(r.get("_event_index")) is int else -1)):
        try:
            raw_id = d.get("id")
            bounded_id = _card_id(raw_id)
            if bounded_id is None:
                continue
            c = cards.get(_canon(bounded_id))
        except Exception:  # noqa: BLE001 — a malformed id must not brick the fold
            c = None
        if c is None:
            continue
        for k, v in d.items():
            if k in _ENRICH_DICT and isinstance(v, dict):
                valid, bounded = _bounded_card_enrichment(v)
                if valid:
                    setattr(c, k, bounded)
            elif k == "concept_tags" and isinstance(v, list):
                if c.concept_source is not None and c.concept_source.kind == "node":
                    continue
                refs: list[str] = []
                for item in v[:64]:
                    if isinstance(item, str) and item not in refs:
                        refs.append(item)
                c.concept_tags = refs
                c.concept_source = _proposal_card_concept_source(
                    "card_enriched", present=True,
                    overflow=d.get("_concept_tags_overflow") is True,
                    invalid=d.get("_concept_tags_invalid") is True,
                )
                # CODEX AGENT: enrichment is proposal metadata, never independent classifier/operator
                # evidence.  Keep the legacy scalar synchronized with the exact owner receipt.
                c.provenance_tier = None
            elif k in _ENRICH_REFS and isinstance(v, list):
                refs: list[str] = []
                for item in v[:64]:
                    if not isinstance(item, str):
                        continue
                    ref = item.strip()[:400]
                    if ref and ref not in refs:
                        refs.append(ref)
                setattr(c, k, refs)
            elif k == "steering_context" and isinstance(v, list):
                context: list[dict] = []
                for item in v[:64]:
                    if not isinstance(item, dict):
                        continue
                    valid, bounded = _bounded_card_enrichment(item)
                    if valid and isinstance(bounded, dict):
                        context.append(bounded)
                c.steering_context = context
            elif k in _ENRICH_STR and v is not None:
                setattr(c, k, str(v)[:400])
            elif k == "foresight_rank" and v is not None:
                try:
                    rank = int(v)
                except (TypeError, ValueError):
                    pass
                else:
                    if not isinstance(v, bool) and 0 <= rank < 256:
                        c.foresight_rank = rank
            elif k == "confidence" and v is not None:
                try:
                    confidence = float(v)
                except (TypeError, ValueError, OverflowError):
                    pass
                else:
                    if (not isinstance(v, bool) and math.isfinite(confidence)
                            and 0.0 <= confidence <= 1.0):
                        c.confidence = confidence

    # Board priority — the explicit `card_ranked` order, else the `hypothesis_ranking` shadow (both stamp
    # the OPEN lane's 0-based position, mirroring `_derive_hypotheses`; None once a card resolves).
    native_card_ranking = st.card_ranking is not None
    order = (st.card_ranking or st.hypothesis_ranking or {}).get("order") or []
    if native_card_ranking:
        # A native card_ranked event owns the foresight projection, including clearing a prior explicit
        # enrichment for cards it no longer ranks.
        for c in cards.values():
            c.foresight_rank = None
    ranked_cards: set[str] = set()
    for raw_id in order:
        bounded_id = _card_id(raw_id)
        if bounded_id is None or (not native_card_ranking and bounded_id in ambiguous_seeds):
            continue
        canonical_id = _canon(bounded_id)
        if canonical_id in ranked_cards:
            continue
        rank_i = len(ranked_cards)
        ranked_cards.add(canonical_id)
        c = cards.get(canonical_id)
        if c is not None and c.verdict == "open":
            c.priority = rank_i
            if native_card_ranking or c.foresight_rank is None:
                c.foresight_rank = rank_i

    # 7) operator-override overlay — RESERVED FINAL PHASE (docs/23 decision 27: the operator wins
    #    regardless of event arrival order). The maps are empty until Layer 6 fills them, so this is a
    #    no-op in Layer 1; reserving it here means Layer 6 needs no `_derive_cards` rewrite. `card_edited`
    #    overlays the DISPLAY statement only — the join key stays `seed_statement` (docs/23 decision 24).
    for cid, edit in (st.card_operator_edits or {}).items():
        c = cards.get(cid)
        if c is not None and isinstance(edit, dict) and edit.get("statement"):
            c.statement = str(edit["statement"])
    for cid, pri in (st.card_priority_pins or {}).items():
        c = cards.get(cid)
        if c is not None:
            try:
                c.priority = int(pri)
            except (TypeError, ValueError):
                pass
    for cid, pin in (st.card_resource_pins or {}).items():
        c = cards.get(cid)
        if c is not None and isinstance(pin, dict):
            fp = dict(c.footprint or {})
            for k in ("gpus", "gpu_mem_mib"):
                if k in pin:
                    fp[k] = pin[k]
            fp["pinned_by"] = "operator"
            c.footprint = fp

    # 8) LAYER-1c exclusion seam — derive `actionable` from the FINAL status/verdict (after every
    #    override). This compatibility flag means only "not administratively dead" for the board:
    #    running/evaluated cards intentionally remain True. It MUST NOT be consumed as proof of
    #    executability; receipt-backed `selection_ready` below is the future queue seam.
    for c in cards.values():
        c.actionable = c.status not in ("dropped", "gated") and c.verdict != "abandoned"

    # CODEX AGENT: Step 9 fails closed at the architecture boundary. Hypothesis remains a
    # research-direction aggregate; a selectable Card must be exactly one immutable work item with a
    # durable `card_added`
    # ownership receipt. No current production writer emits that receipt, so legacy hash joins, unbound
    # card_added rows, and node-only card ids remain visible but can never become selection-ready.
    breedable_card_parent_ids = {node.id for node in st.breedable_nodes()}
    for cid, c in cards.items():
        registration = card_registrations.get(cid, {})
        if (registration.get("count") == 1 and registration.get("valid_count") == 1
                and isinstance(registration.get("digest"), str)):
            c.identity = CardIdentityProvenance(
                kind="native", source="card_added_receipt", durable=True, receipt_valid=True,
                action_digest=registration["digest"],
            )
        else:
            origin = card_origins.get(cid, "unknown")
            legacy = origin in {"hypothesis_shadow", "node_statement_hash"}
            c.identity = CardIdentityProvenance(
                kind="legacy_hash" if legacy else "synthesized_shadow",
                source=origin if origin in {
                    "card_added_unbound", "hypothesis_shadow", "node_statement_hash",
                    "node_card_id", "merge", "unknown",
                } else "unknown",
            )

        owner = action_owners.get(cid, {"count": 0, "sources": set(), "all_complete": False})
        owner_count = min(257, owner["count"])
        owner_sources = owner["sources"]
        if owner_count == 0:
            action_source = "none"
        elif len(owner_sources) == 1:
            action_source = next(iter(owner_sources))
        else:
            action_source = "mixed"

        projected_digest = card_action_digest(
            c.id, c.seed_statement, _card_action_from_projection(c))
        action_complete = bool(
            owner_count == 1
            and owner["all_complete"]
            and c.identity.kind == "native"
            and projected_digest == c.identity.action_digest
            and _card_action_has_live_anchors(c, breedable_card_parent_ids)
        )
        if c.scored_against is None or st.best_node_id is None:
            freshness = "unknown"
        elif c.scored_against == st.best_node_id:
            freshness = "current"
        else:
            freshness = "stale"

        work_states: set[str] = set()
        for node_id in c.evidence:
            node = st.nodes.get(node_id)
            if node is None:
                work_states.add("unknown")
            elif (node.status is NodeStatus.pending and not node.tombstoned
                  and node.id not in st.aborted_nodes):
                work_states.add("in_flight")
            else:
                work_states.add("terminal")
        if not work_states:
            owner_state = "none"
        elif len(work_states) == 1:
            owner_state = next(iter(work_states))
        elif "unknown" in work_states:
            owner_state = "unknown"
        else:
            owner_state = "mixed"
        c.selection_provenance = CardSelectionProvenance(
            action_source=action_source,
            action_owner_count=owner_count,
            action_complete=action_complete,
            freshness=freshness,
            owner_state=owner_state,
        )

        blockers: list[str] = []
        if c.identity.kind != "native":
            blockers.append("identity_not_native")
        if owner_count == 0:
            blockers.append("action_owner_missing")
        elif owner_count > 1:
            blockers.append("action_owner_ambiguous")
        if owner_count == 1 and not action_complete:
            blockers.append("action_receipt_incomplete")
        if freshness == "unknown":
            blockers.append("freshness_unknown")
        elif freshness == "stale":
            blockers.append("freshness_stale")
        if owner_state in {"in_flight", "mixed"}:
            blockers.append("work_in_flight")
        if owner_state in {"terminal", "mixed"}:
            blockers.append("work_terminal")
        if owner_state == "unknown":
            blockers.append("work_owner_unknown")
        if c.status in {"dropped", "gated"} or c.verdict == "abandoned":
            blockers.append("card_terminal")
        work_item_aliases = [
            alias_id for alias_id in c.aliases
            if not c.seed_statement or alias_id != hypothesis_id(c.seed_statement)
        ]
        if work_item_aliases:
            blockers.append("merged_work_items")
        c.selection_blockers = blockers
        c.selection_ready = not blockers

    # A hypothesis DELETED by the operator (hypothesis_updated status=deleted) is removed from the board
    # entirely; the shadow card must vanish with it (mirrors `_derive_hypotheses`' final filter). Until a
    # card-native delete exists, reuse `hypotheses_deleted`; card ids == hypothesis ids in Layer 1a.
    st.cards = {
        cid: card for cid, card in cards.items()
        if not any(control_id in st.hypotheses_deleted
                   for control_id in control_ids.get(cid, {cid}))
    }
