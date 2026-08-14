"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. ``best_node_id`` is a deterministic post-pass over eligible
evaluated nodes using trust/fitness treatment, confirmation, holdout and approval state; node id is
only the final tie-break. No separate ``best_updated`` event is needed.
"""
from __future__ import annotations

import heapq
import math
from typing import Iterable, Optional

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
    normalized_concept_renames,
    resolve_concept_set_reasons,
)
from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, SearchFitness, finite_metric,
                                  is_usable_metric,
                                  verifier_evidence_digest)
from looplab.core.jsonutil import valid_digest_ref
from looplab.core.models import (CARD_STATEMENT_MAX_UTF8_BYTES as _CARD_REPLAY_STATEMENT_MAX_BYTES,
                     NODE_CONCEPT_PROVENANCE_AUTHORED,
                     NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR,
                     NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                     NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                     INHERITABLE_CONCEPT_PROVENANCE as _INHERITABLE_CONCEPT_PROVENANCE,
                     node_concept_event_provenance,
                     Event, Idea, Node, NodeStatus, RunState, Trial,
                     coerce_node_id as _coerce_node_id,
                     hypothesis_id,
                     normalize_extra_metrics, normalize_researcher_footprint,
                     normalize_steering_context,
                     run_setup_key)
# The derived Card ledger (doc 25 EV-01). ONLY the names this module's own handlers call are
# imported: a re-export of a helper `card_ledger` then calls internally would look like a patch seam
# while a monkeypatch through it silently missed the fold, which is the exact failure the flat-import
# alias shim exists to prevent for MODULES. Tests that reach for a ledger internal import it from
# `looplab.events.card_ledger` directly.
from looplab.events.card_ledger import (
    CARD_ENRICHMENT_JOURNAL_MAX,
    _CARD_REPLAY_NODE_ID_MAX,
    _CARD_REPLAY_STATEMENT_MAX,
    _bounded_card_added_receipt,
    _bounded_card_cross_run_enrichment,
    _bounded_card_drop_receipt,
    _bounded_card_enrichment,
    _bounded_card_footprint_enrichment,
    _bounded_card_merge_receipt,
    _bounded_card_novelty_enrichment,
    _bounded_card_ref,
    _card_replay_id,
    _card_replay_node_id,
    _card_replay_text,
    _digest_ref,
    derive_cards as _derive_cards,
)
from looplab.events.comment_projection import apply_comment_event
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_ANNOTATION, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_BUDGET_EXTEND, EV_CONFIRM_DONE,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_CONFIRM_EVAL, EV_DATA_LEAKAGE, EV_DATA_PROFILED, EV_DATA_PROVENANCE, EV_ENV_CHANGED,
    EV_CONCEPT_COVERAGE_SNAPSHOT, EV_COVERAGE_SNAPSHOT, EV_DEEP_RESEARCH, EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM,
    EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_BUILD_ATTEMPTED, EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED, EV_CARD_DROPPED,
    EV_CARD_EDITED, EV_CARD_ENRICHED, EV_CARD_MERGED, EV_CARD_RANKED,
    EV_CARD_REPRIORITIZED, EV_CARD_RESOURCE_PINNED,
    EV_FORESIGHT_SELECTED, EV_FORK,
    EV_FORK_DONE, EV_HINT, EV_HOLDOUT_EVALUATED, EV_HOST_GRADING, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
    EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED, EV_INJECT_DONE, EV_INJECT_NODE, EV_LESSONS_DISTILLED,
    EV_LESSONS_REFRESHED, EV_LLM_COST, EV_LLM_USAGE, EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_CONCEPT_CONSOLIDATION, EV_CONCEPT_EDGE, EV_CONCEPT_TAG_EDITED,
    EV_HYPOTHESIS_CONCEPTS, EV_NODE_CONCEPTS, EV_RUN_CONCEPTS,
    EV_NODE_CREATED, EV_NODE_EVAL_STARTED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED,
    EV_NODE_RESET,
    EV_CROSS_RUN_PRIOR,
    EV_NODE_TOMBSTONED, EV_NODE_VERIFIED, EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED, EV_PAUSE, EV_STAGE_FINISHED,
    EV_POLICY_DECISION, EV_PROMOTE, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_ATTEMPTED, EV_RESEARCH_COMPLETED, EV_RESTART, EV_RESUME, EV_RESUME_REQUESTED,
    EV_RESUME_SERVED,
    EV_REWARD_HACK_SUSPECTED, EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED, EV_RUN_STARTED,
    EV_RUN_WIDTH_SETTLED,
    EV_RUNG_PROMOTED,
    EV_SET_STRATEGY,
    EV_SETUP_FINISHED, EV_SPEC_APPROVAL_REQUESTED, EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED,
    EV_SPECULATION_DEPTH_SETTLED,
    EV_STRATEGY_DECISION, EV_TRUST_GATE_CHANGED, EV_VERIFIER_GROUP_SCORED, EV_WORKSPACE_CHANGED,
    standing_hint_dedup_key)


# 9999-12-31T23:59:59Z. Past this an `Event.ts` is corruption (or a unit mix-up — a milliseconds
# timestamp lands here), not a date, and admitting it would let one damaged row define a run's whole
# duration. Paired with the `> 0` floor below because `Event.ts` DEFAULTS to 0.0: a hand-built Event
# or a fixture that never went through `EventStore.append` carries "no timestamp", not 1970.
_MAX_EVENT_TS = 253_402_300_799


def event_timestamp(e) -> Optional[float]:
    """One event's wall-clock timestamp as a usable float, or None when the row does not carry one.

    The ONE spelling of that rule, because two readers need the same answer over the same untrusted
    bytes and used to derive it separately: `_on_report` publishes `published_at` from it, and
    `run_wall_clock_seconds` below measures a run's duration from it. `type(ts) in (int, float)`
    rather than `isinstance` on purpose — `isinstance(True, int)` is True, and a JSON `true` in a
    hand-edited log would otherwise become the epoch second 1.
    """
    ts = getattr(e, "ts", None)
    if type(ts) not in (int, float) or not math.isfinite(ts):
        return None
    return float(ts) if 0 < ts <= _MAX_EVENT_TS else None


def run_wall_clock_seconds(events: Iterable[Event]) -> Optional[float]:
    """How long the RUN took, from its own log: last usable `ts` minus first usable `ts`.

    This is the one duration that survives a process boundary. A run that is stopped and wrapped up
    hours later by `looplab finalize` is finished by a DIFFERENT process, so any `time.time() - start`
    the finalizing process measures describes the wrap-up, not the run — measured, a 274-second run
    reported `budget.elapsed_s = 0.027`. The event log is the only record that spans both processes,
    and it has carried `ts` on every row since the first version of the envelope, so this is exact on
    OLD logs too — nothing had to be recorded for it.

    Order-tolerant (min/max, not first/last position) like everything else that reads the log, and
    reader-tolerant: rows without a usable timestamp are skipped rather than dragging the span to
    1970. Returns None when NO row carries one (a synthetic/hand-built log), so a caller can say
    "unknown" instead of publishing a confident 0.0.

    Note what it deliberately does NOT do: subtract the idle gap while a stopped run waited for its
    `finalize`. That gap is part of how long the run took, and it is exactly the interval the old
    number pretended did not exist. `looplab timings` names the untraced share of it.
    """
    first: Optional[float] = None
    last: Optional[float] = None
    for e in events:
        ts = event_timestamp(e)
        if ts is None:
            continue
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
    if first is None or last is None:
        return None
    return max(0.0, last - first)


def flagged_node_ids(st: RunState) -> set:
    """T2: node ids excluded from best/holdout selection under trust_gate gate/block — those with a
    HIGH-PRECISION cheating/leakage signal (see `is_hard_signal`). One `critic:` signal —
    `critic:hardcoded_metric` — is HARD and gates; every OTHER `critic:` issue and `perfect_metric`
    stay advisory in every mode (perfect_metric flags the EXACT theoretical optimum — metric==0.0 on
    min / ==1.0 on max — which a legitimately-perfect score hits, so gating on it could exclude honest
    winners). Empty under `audit`. Shared by the fold and the engine's holdout-topk so both apply the
    SAME exclusion."""
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
    # An unknown FUTURE signal name stays hard on purpose (fail closed toward catching cheating).
    # A BLANK one is different: it is not a signal at all, only the `s.get("signal", "")` default for
    # an entry that never carried the key. Counting it as high-precision cheating evidence let a
    # single malformed/hand-edited record gate-exclude an honest winner under "gate"/"block" — and
    # the digest then rendered it as `node 1 ()`, the contentless warning this function's own
    # contract says can never happen. Reject the malformed shape; keep every named signal hard.
    sig = sig.strip() if isinstance(sig, str) else ""
    if not sig:
        return False
    return not sig.startswith(("critic:", "perfect_metric", "protected_audit_unavailable",
                               "suspicious_output"))


def hard_flagged_ids(st: RunState) -> set:
    """Node ids carrying a HIGH-PRECISION cheating/leakage signal, including the narrow
    ``critic:hardcoded_metric`` exception but excluding other ``critic:*`` and ``perfect_metric``
    heuristics, INDEPENDENT of `trust_gate` mode. `flagged_node_ids` uses it for gate/block selection
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
        "card_enrichment_index", "card_enrichment_omissions",
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
        # Index retained enrichment candidates so an attacker-sized set of distinct owners remains
        # O(events), and count rejected candidates so public completeness can fail closed.
        self.card_enrichment_index: dict[tuple, int] = {}
        self.card_enrichment_omissions: dict[tuple, int] = {}


def _settle_folded_speculation_depth(st: RunState) -> None:
    """Recompute the EFFECTIVE Layer-5 depth from the run's two independent depth facts.

    `speculation_depth_pinned` is `run_started`'s launch treatment and `speculation_depth_settled` is
    the floor the run's own adaptive ratchet has narrowed itself to; the effective depth is the pin
    capped by that floor. Both writers call this, which is what makes the pair ORDER-TOLERANT: each
    fact is written by exactly one handler, neither reads the other's field before writing its own,
    and the derivation is a pure minimum. Splicing `speculation_depth_settled` anywhere relative to
    `run_started` therefore lands on the same effective depth — which a minimum taken directly on
    `speculation_depth` did NOT: `run_started` ASSIGNS over a `RunState` default of 0, so a settle
    row folded first was simply overwritten (measured: 4 at position 0, 0 at every other position).

    A floor ABOVE the pin is inert, which is what keeps a stale/foreign/hand-edited row from ever
    RAISING the treatment — the property the minimum was chosen for.
    """
    floor = st.speculation_depth_settled
    st.speculation_depth = (st.speculation_depth_pinned if floor is None
                            else min(st.speculation_depth_pinned, floor))


def _on_run_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FIRST START WINS. `run_started` is the one-time identity anchor and carries the run's immutable
    # authority: `direction` (champion ordering for the whole run), `trust_gate`, `card_driven_selection`,
    # `speculation_depth` and its gate receipts. Last-write-wins let a spliced/duplicated second row
    # INVERT the objective or relax the trust gate after nodes already existed — silently rewriting how
    # every prior result is ranked. This gate mirrors the producer exactly: the engine appends
    # `run_started` only `if not state.run_id` (orchestrator.py:2307), so on any log it wrote this is a
    # no-op. Keyed on `run_id` rather than "have I seen one" for the same reason: a row that never
    # established identity is not an anchor and must not shadow the real start that follows it.
    if st.run_id:
        return
    # Read with defaults like every other fold handler (RunState already defaults these to ""): the
    # fold loop dispatches handlers with NO per-event try/except, so a bare d["run_id"] KeyError on a
    # malformed/hand-edited run_started would take down the WHOLE fold (every view/replay/resume of the
    # run) — the exact hand-edited-log-tolerance the _on_node_created guard was added to provide.
    st.run_id = d.get("run_id", "")
    _run_uid = d.get("run_uid", "")
    st.run_uid = _run_uid if isinstance(_run_uid, str) else ""
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
    # F1d: the run-level DECLARED ENVIRONMENT the evals ran under. Absent on old logs and on every
    # run that declared none -> `{}` -> the engine keeps its own launch value, i.e. byte-identical
    # legacy behaviour. Coerced to `{str: str}` here rather than trusted: this is read back by
    # `Engine._repin_declared_env` and handed to a child PROCESS, so a hand-edited row carrying a
    # list or a nested object must degrade to "nothing declared" instead of reaching `subprocess`
    # as an unusable env dict — the fold has no exception handler and cannot raise.
    _ee = d.get("eval_env")
    st.eval_env = ({str(k): str(v) for k, v in _ee.items()
                    if isinstance(k, str) and isinstance(v, (str, int, float))
                    and not isinstance(v, bool)}
                   if isinstance(_ee, dict) else {})
    # Layer 3 queue ownership is selection-affecting and therefore pinned by the event log. Accept
    # only the JSON boolean true: strings and integers in malformed/legacy rows fail closed to the
    # byte-identical policy/pilot path.
    st.card_driven_selection = d.get("card_driven_selection") is True
    # A strict bounded integer is required: bools/strings/floats are malformed and must not turn on
    # speculative execution. Absent on old logs -> 0 -> historical alternating build/eval behavior.
    # This is the LAUNCH PIN and nothing else writes it; the EFFECTIVE depth is derived from it and
    # the adaptive floor by `_settle_folded_speculation_depth`, so this handler and
    # `_on_speculation_depth_settled` may land in either order (invariant #5).
    _spec_depth = d.get("speculation_depth", 0)
    st.speculation_depth_pinned = (
        _spec_depth if type(_spec_depth) is int and 0 <= _spec_depth <= 64 else 0)
    # Whether that pin RESOLVED the AUTO sentinel or was SPELLED. `is True` rather than `bool(...)`:
    # only the literal the writer emits may enable the one-way ratchet, so a truthy string or a 1 in
    # a hand-edited log cannot turn someone's spelled treatment into a self-narrowing one. Absent
    # folds to False — see the field's comment in `core/models.py`.
    st.speculation_depth_auto = d.get("speculation_depth_auto") is True
    _settle_folded_speculation_depth(st)
    # Four sha256-prefixed receipt digests admitted by ONE predicate (doc 25 EV-04); they were four
    # copies of the same six-line conjunction. The assignments stay written out rather than a
    # `setattr` loop over field-name strings: a typo in such a loop would silently set the wrong
    # attribute AND leave the real one at its default — the silent-no-op class this fold exists to
    # avoid. The fail-closed "" per field is what makes a malformed digest read as "no receipt"
    # rather than as a receipt nothing can verify.
    _rd = d.get("speculation_gate_receipt_digest", "")
    st.speculation_gate_receipt_digest = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_runtime_scope_sha256", "")
    st.speculation_runtime_scope_sha256 = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_implementation_digest", "")
    st.speculation_implementation_digest = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_calibration_profile_digest", "")
    st.speculation_calibration_profile_digest = (
        _rd if valid_digest_ref(_rd, prefix="sha256:") else "")
    # Bound the row CONTENTS, not just the row count: `dict(row)` copied each row whole, so a
    # hand-edited/foreign run_started could park megabytes in RunState — which FoldCursor then
    # deep-copies on EVERY snapshot. That is the amplification the card handlers' bounding comments
    # warn about, and this was the one fold boundary here without it. The bound is deliberately far
    # above the real schema (`speculation_quality._GPU_IDENTITY_FIELDS` is 7 fields of short
    # scalars, and that consumer REJECTS anything else), so no legitimate log changes shape;
    # oversized keys/values are dropped rather than truncated, because a silently shortened uuid or
    # pci_bus_id would be a different GPU, and the consumer must see the row fail its schema.
    _calibration_inventory = d.get("speculation_calibration_gpu_inventory", [])
    st.speculation_calibration_gpu_inventory = (
        [_bounded_gpu_inventory_row(row) for row in _calibration_inventory]
        if isinstance(_calibration_inventory, list)
        and len(_calibration_inventory) <= 256
        and all(isinstance(row, dict) for row in _calibration_inventory)
        else []
    )
    _calibration_seed = d.get("speculation_calibration_seed")
    st.speculation_calibration_seed = (
        _calibration_seed
        if type(_calibration_seed) is int and 0 <= _calibration_seed <= (1 << 63) - 1
        else None
    )
    _policy_scope = d.get("speculation_policy_scope", "")
    st.speculation_policy_scope = _policy_scope if _policy_scope == "greedy" else ""
    # The SETTLED concurrency widths, pinned as RESOLVED integers (never the `0` AUTO sentinel, which
    # re-derives off the resuming box). Strict bounded ints for the same reason speculation_depth is:
    # a bool/string/float in a hand-edited or foreign row must not reshape a run's execution
    # treatment. Absent (old logs) or malformed -> 0 -> "not recorded" -> the engine keeps its own
    # startup resolution, which is byte-identical to the pre-pin behaviour.
    _eval_parallel = d.get("eval_parallel", 0)
    st.eval_parallel = (_eval_parallel if type(_eval_parallel) is int
                        and 0 <= _eval_parallel <= 1024 else 0)
    _llm_parallel = d.get("llm_parallel", 0)
    st.llm_parallel = (_llm_parallel if type(_llm_parallel) is int
                       and 0 <= _llm_parallel <= 64 else 0)
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
    if nid in st.aborted_nodes or (current is not None and current.tombstoned):
        _clear_build_marker(st, d, nid)
        return
    if current is not None and not _generation_matches(current, d):
        return
    marker = {"node_id": nid, "operator": d.get("operator"),
              "parent_ids": d.get("parent_ids", []), "started": e.ts}
    card_id = _card_replay_id(d.get("card_id"))
    if card_id is not None:
        # Additive link for the Card queue. Keep malformed/oversized ids out of the transient
        # RunState marker just as the durable card journals do; old node_building rows retain their
        # exact marker shape because the key is absent unless a valid id was recorded.
        marker["card_id"] = card_id
    if d.get("speculative") is True:
        card_build_generation = d.get("card_build_generation")
        if (type(card_build_generation) is int
                and 0 <= card_build_generation <= _CARD_REPLAY_NODE_ID_MAX):
            # This is the speculative request epoch, distinct from the Node lifecycle generation
            # below. Keeping both names prevents a reopened-run request from impersonating another
            # request merely because every newly-created Node starts at lifecycle generation zero.
            marker["speculative"] = True
            marker["card_build_generation"] = card_build_generation
    generation = _event_generation(d)
    if type(generation) is int and generation >= 0:
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
    # log) must not crash the WHOLE fold — skip the bad event instead (normal engine/control writers
    # round-trip validated payloads, so this only fires on a corrupt or manually spliced log).
    if not _parent_generation_map_matches(st, d):
        _clear_build_marker(st, d, nid)
        return
    current = st.nodes.get(nid)
    # A generation-less abort may deliberately name the next not-yet-created slot. Only the main
    # writer can acknowledge that pre-reservation intent with this narrow marker; ordinary late
    # workers remain inert after an abort, preserving the unknown-abort resurrection fence.
    materialize_aborted_intent = bool(
        d.get("materialize_aborted_intent") is True
        and current is None
        and nid in st.aborted_nodes
        and _event_generation(d) is _MISSING
    )
    if ((nid in st.aborted_nodes and not materialize_aborted_intent)
            or (current is not None and current.tombstoned)):
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
    # `d.get("parent_ids", [])` defaults only when the KEY IS ABSENT, so an explicit
    # `"parent_ids": null` — the natural JSON spelling for a root node — reached the comprehension as
    # None and raised TypeError. This runs BEFORE the `try:` that exists to make one corrupt node row
    # survivable, so it bricked every later fold/replay/resume/view of the run instead of skipping
    # that event. Guard the type like `_on_node_repaired` and `_parent_generation_map_matches`
    # already do (fold must stay total).
    raw_parent_ids = d.get("parent_ids")
    parent_ids = [
        parent_id
        for raw_parent_id in (raw_parent_ids if isinstance(raw_parent_ids, list) else [])
        if (parent_id := _coerce_node_id({"node_id": raw_parent_id})) is not None
    ]
    speculative = d.get("speculative") is True
    raw_card_build_generation = d.get("card_build_generation")
    card_build_generation = (
        raw_card_build_generation
        if (speculative and type(raw_card_build_generation) is int
            and 0 <= raw_card_build_generation <= _CARD_REPLAY_NODE_ID_MAX)
        else None
    )
    try:
        n = Node(
            id=nid,
            parent_ids=parent_ids,
            # `_parent_generation_map_matches` proved each parent exists at this event boundary. Capture
            # that boundary even for legacy/mapless rows, otherwise a later parent reset makes provenance
            # point at replacement bytes the child never used.
            parent_generations={
                str(parent_id): st.nodes[parent_id].attempt
                for parent_id in parent_ids
            },
            operator=d["operator"],
            idea=Idea(**d["idea"]),
            code=d.get("code", ""),
            files=d.get("files", {}) or {},
            deleted=d.get("deleted", []) or [],
            attempt=generation,
            origin=d.get("origin"),   # cross-run provenance (None for ordinary nodes)
            research_origin=d.get("research_origin"),   # 💡 proposed just after a deep-research memo
            footprint_finalized=d.get("footprint_finalized") is True,
            speculative=speculative,
            card_build_generation=card_build_generation,
            # The writer's promise that this lifecycle gets a durable eval-START row before any
            # sandbox work (events/types.py::EV_NODE_EVAL_STARTED). Only a node whose creator made
            # that promise may later be REFUNDED on the absence of one; an old log carries no promise
            # and is charged. Additive + reader-defaulted -> old logs fold byte-identically.
            eval_start_boundary=d.get("eval_start_boundary") is True,
        )
    except (MemoryError, RecursionError):
        # A RESOURCE glitch is NOT a corrupt-data error: it must fail LOUD, not be swallowed.
        # A MemoryError silently caught here drops the node -> fold returns empty nodes ->
        # `_create_node` re-computes node_id=0 forever -> a 184MB node_created(0) runaway. Let
        # it propagate so a transient glitch surfaces instead of self-sustaining into a spin.
        raise
    except Exception:
        return   # (was `continue` in the loop arm: skip just this event)
    st.nodes[n.id] = n
    _fold_node_concept_envelope(st, ctx, n, d, current)
    if current is None:
        # A holdout score is a disclosed final-exam signal. If a genuinely NEW candidate lands
        # afterwards (an inject/fork/policy action won the finish CAS race), the search has become
        # adaptive to that signal. Rotate the hidden split before any later promotion can reuse it.
        _invalidate_disclosed_holdout(st, fresh_node_ids={n.id})
        # A genuinely new candidate invalidates any confirmation/approval completed for the prior
        # candidate set — including when it is created just AFTER best_confirmed was appended.
        _invalidate_completion_certificates(st, ctx)
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


# One selection-affecting scalar normalizer, shared with `search/speculation_quality` (doc 25 SE-08).
_finite_metric = finite_metric


def _normalize_resource_curve(raw):
    """Coerce untrusted node_evaluated `resource_curve` event data (#7 review) to at most 32 sorted,
    unique, finite `[resource, metric]` pairs, or None. Node assignment validation is off, so a hand-
    edited / corrupt / future log could otherwise land a scalar or an arbitrarily large nested value on
    the Node despite the promised 32-point bound. Invalid entries are dropped and the <=32 bound is
    re-enforced HERE, independently of the writer. A VALID log never exceeds 32 points (the writer
    `extract_resource_curve` already caps it), so the overflow branch below fires only on already-corrupt
    input; there it keeps both endpoints via even spacing — it does NOT reproduce the writer's
    earliest-N-plus-last shape, but the shapes can only differ on input the writer could never emit."""
    if not isinstance(raw, list):
        return None
    by_resource: dict[float, float] = {}
    for entry in raw:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            continue
        r = _finite_metric(entry[0])
        v = _finite_metric(entry[1])
        if r is not None and v is not None:
            by_resource[r] = v
    if not by_resource:
        return None
    coords = sorted(by_resource)
    if len(coords) > 32:                 # corruption guard: keep both endpoints on the (invalid) overflow
        step = (len(coords) - 1) / 31
        coords = [coords[i] for i in sorted({int(round(i * step)) for i in range(32)})]
    return [[r, by_resource[r]] for r in coords]


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
    `st.buildings` entry, each gated on its own generation. Under concurrent build fan-out the singular
    field holds only the last-appended build, so an EARLIER concurrent build's terminal matches its
    `st.buildings` entry but NOT the singular; keying each off its own marker is exactly what stops
    that entry from leaking a stale breathing 'building…' ghost."""
    if _building_matches_event(st, d, nid):
        st.building = None
    if _marker_matches_event(st.buildings.get(nid), d, nid):
        st.buildings.pop(nid, None)


def event_generation_binds(d: dict, generation: int, *, legacy_attempt: bool = False) -> bool:
    """Does the lifecycle stamp on RAW event data `d` bind to `generation`?

    PUBLIC because a raw-log reader outside `events/` needs exactly this question and there must be
    one answer to it. `engine/evaluate.py`'s three durable per-node budgets (repair attempts, dep
    rounds, full re-trains) read `node_repaired`/`deps_installed`/`full_retrain_charged` straight off
    the log rather than through the fold — the fold keeps the latest state, they need the trajectory
    — and each of them hand-spelled this rule as `"generation" in d and d.get("generation") !=
    generation` under a comment claiming it keyed "exactly as `replay._generation_matches` keys it".
    It did not: measured over 18 raw values, `generation: true` was admitted by the `!=` and dropped
    by the fold (`bool` subclasses `int`, so `True != 1` is False, while `coerce_node_id` rejects a
    bool on purpose), and `generation: "1"` was the reverse. A budget charged against rows the fold
    does not have is not the log's budget, which is that family's whole premise.

    Exported rather than declared in `tests/test_cross_package_private_seams.py`, per that registry's
    own rule ("the moment to ask whether it should be public instead"): the alternative was to leak
    `_event_generation` AND the `_MISSING` sentinel across the package boundary, and a sentinel is
    not an API. `_generation_matches` is the Node-side twin and now delegates here, so there is one
    implementation and no second copy to drift.
    """
    stamped = _event_generation(d, legacy_attempt=legacy_attempt)
    return stamped is _MISSING or (stamped is not None and stamped == generation)


def _generation_matches(n: Node, d: dict, *, legacy_attempt: bool = False) -> bool:
    return event_generation_binds(d, n.attempt, legacy_attempt=legacy_attempt)


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
            # ASHA past-experiment curve (#7): a bounded [[resource, metric], ...] the ASHA watchdog
            # reads to find same-resource peers for an EARLY live sample (the 500-char stdout_tail keeps
            # only the final epochs). Reader-defaulted to None so pre-#7 logs fold byte-identically.
            # NORMALIZED (#7 review): assignment validation is off, so an untrusted/corrupt event could
            # otherwise land a scalar or huge nested value here despite the 32-point bound; coerce it.
            n.resource_curve = _normalize_resource_curve(d.get("resource_curve"))
            n.eval_seconds = d.get("eval_seconds")
            n.extra_metrics = normalize_extra_metrics(d.get("extra_metrics"))
            n.violations = d.get("violations", []) or []
            n.feasible = not n.violations       # #5: constraint-violating -> infeasible
            # Additive with a reader-side default: an old log has no such key and folds to None,
            # which is what a measured metric means here.
            _prov = d.get("metric_provenance")
            n.metric_provenance = _prov if isinstance(_prov, dict) else None
            # Intra-node sweep: per-trial results (audit/UI only; node.metric is already the
            # best trial, set by the engine). Coerce defensively per trial so one malformed
            # entry in a hand-edited/bring-your-own-script log can't crash the whole fold.
            # Event.data is untyped and assignment validation is off, so `trials` itself can arrive as a
            # bare scalar (int/float/bool); the per-trial try/except only guards a bad ITEM, so a
            # non-iterable CONTAINER would raise TypeError OUTSIDE it and poison every replay/resume with
            # no JSON divergence for repair-log to see. Require a list/tuple before iterating — the same
            # defence the resource_curve normalization above applies for the identical reason.
            _raw_trials = d.get("trials", [])
            trials = []
            for t_d in (_raw_trials if isinstance(_raw_trials, (list, tuple)) else []):
                try:
                    trials.append(Trial(**t_d))
                except Exception:
                    continue
            n.trials = trials
            _charge_terminal_cost(st, n, d, ctx)


_FAILURE_SPIKE_IGNORED_REASONS = {
    "aborted", "cancelled", "card_dropped", "proxy_skipped", "superseded",
}


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
            # COERCED, because assignment skips pydantic validation and `_card_debuggable_leaf_ids`
            # later does `node.error_reason not in {"idea_rejected", "card_dropped"}` — a SET
            # membership — inside `_derive_cards`. One node_failed row carrying an unhashable reason
            # (a list/dict from a forged, foreign or hand-edited log) therefore made every
            # fold/replay/resume of that run raise TypeError, forever. Same totality rule the rest of
            # this handler follows.
            _reason = d.get("reason", "")
            n.error_reason = _reason if isinstance(_reason, str) else str(_reason)
            # Crash-triage verdict, when the LLM triage ran (signal-delivery §1): fold it onto
            # the node so the failure-reflection hint / digest can hand it to the next proposal.
            # Additive + reader-defaulted: absent on old logs / rule-triaged nodes -> stays "".
            if d.get("triage_rationale"):
                n.triage_rationale = str(d.get("triage_rationale"))
            n.eval_seconds = d.get("eval_seconds")
            # Durable "no evaluation was ever dispatched for this lifecycle" receipt (Node.
            # never_evaluated). Additive + reader-defaulted: absent on old logs -> False -> the budget
            # accounting folds byte-identically. Only the writers that terminalize a build BEFORE
            # dispatch stamp it, and it rides this single terminal, so "first terminal wins" above is
            # the whole of its order-tolerance argument.
            n.never_evaluated = d.get("never_evaluated") is True
            n.rerun_from = None
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            if d.get("failed_stage"):
                n.failed_stage = d.get("failed_stage")   # Phase 1: which pipeline stage broke
            _charge_terminal_cost(st, n, d, ctx)
            _add_current_failure(st, n, e)

def _on_node_eval_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """The durable eval-START boundary: this exact lifecycle entered the sandbox.

    Set-only and generation-keyed, so it is order-tolerant by construction: a duplicate (a resumed
    process re-dispatching the same still-pending node) is a no-op, a row for an abandoned attempt is
    ignored by `_generation_matches`, and a `node_reset` clears the flag with the rest of the
    lifecycle it abandons.  Carries no cost — `_charge_terminal_cost` still owns eval seconds; this
    only answers "did anything run at all", which nothing else in the log could answer after a kill.
    """
    n = _node_for_event(st, d)
    if n is not None and _generation_matches(n, d):
        n.eval_started = True

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
        if isinstance(d.get("idea_footprint"), dict):
            footprint = normalize_researcher_footprint(d["idea_footprint"])
            if footprint is not None:
                n.idea = n.idea.model_copy(deep=True, update={"footprint": footprint})
        if d.get("footprint_finalized") is True:
            n.footprint_finalized = True

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
        n.resource_curve = None            # #7: the prior attempt's curve no longer describes this node
        n.eval_seconds = None
        n.never_evaluated = False          # the discard receipt described the prior attempt
        n.eval_started = False             # ...and so did the eval-start boundary
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        # WHERE THE OLD METRIC CAME FROM described the old metric, which this epoch just cleared.
        # Left set, a node reset then failed read `metric=None, status=failed,
        # metric_provenance={salvaged: True}` — a provenance record for a value that no longer
        # exists, on the one field a reader consults to decide whether to trust the number.
        n.metric_provenance = None
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
    _purge_node_requests(st, requeued)
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


def _clear_approval(st: RunState) -> None:
    """Retract the operator's ratification AND any request still waiting for one.

    Both halves are needed together. Leaving `approved` set hands a stale grant to a candidate set
    the operator never saw; leaving `awaiting_approval` set with the subject gone parks the run on a
    question about a node that no longer exists. The subject/generation/node_id fields are what the
    approval was ABOUT, so they go with it — a retained `approval_subject` would let a later grant
    attach to the wrong node.
    """
    st.approved = False
    st.awaiting_approval = False
    st.approval_subject = None
    st.approval_generation = None
    st.approved_node_id = None


def _invalidate_completion_certificates(st: RunState, ctx: "_FoldCtx") -> None:
    """Retire every "this search is finished" certificate because the candidate set just changed.

    A confirmation and an approval are both statements about a SPECIFIC set of candidates: "these
    were re-measured and this one won", "the operator ratified this one". A new candidate, a
    tombstone, a reset, an abort, or a reopen all change that set, so both statements stop being
    true — and neither is re-derived, they are carried until something clears them.

    Two things must be cleared together, and this is the whole reason the sequence has one home
    (doc 25 EV-03). `st.confirmed_done` is the FOLDED flag that lets the confirm phase re-run;
    `ctx.best_confirmed` is the THREADED snapshot `_select_best`'s confirm-override reads. Clearing
    only the flag leaves the override live, and an epoch-(N-1) certificate then keeps beating
    epoch-N's metric winner — which is exactly the selection bug the reopen site shipped while
    these five copies were kept in step by hand.
    """
    st.confirmed_done = False
    ctx.best_confirmed = None
    _clear_approval(st)


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
    _purge_node_requests(st, affected)
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
    _invalidate_completion_certificates(st, ctx)
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
        n.never_evaluated = False   # the discard receipt described the NOW-abandoned lifecycle
        n.eval_started = False      # ...and so did the eval-start boundary
        n.stdout_tail = ""
        n.resource_curve = None            # #7: the abandoned attempt's curve no longer describes this node
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        # See the same line in `_requeue_partition_bound_results`: the provenance describes the metric this
        # reset just cleared, and a stale `{salvaged: True}` on a failed node is a claim about a
        # number that is gone.
        n.metric_provenance = None
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
        _purge_node_requests(st, {n.id})
        # Abort/proxy decisions belong to the lifecycle that was active when they were recorded.
        # Keeping them would immediately abort/skip every reset generation forever.
        st.aborted_nodes = [nid for nid in st.aborted_nodes if nid != n.id]
        st.proxy_scores.pop(n.id, None)
        st.proxy_skipped = [nid for nid in st.proxy_skipped if nid != n.id]
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
                # the raw delta belongs to the Idea being abandoned. Clear it at the reset
                # boundary itself; otherwise a replay between reset and rebuild rematerializes stale
                # taxonomy for the pending node from a proposal that no longer exists.
                st.node_concept_deltas.pop(n.id, None)
                ctx.concept_mode_untrusted.discard(n.id)
                ctx.concept_input_capped.discard(n.id)
                ctx.concept_input_invalid.discard(n.id)
                # generation stamps did not exist on early classifier events. Remember
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
        # `best_confirmed.generations` covers the whole candidate set. Resetting ANY competitor
        # invalidates the snapshot, even when the previously chosen winner itself was untouched.
        _invalidate_completion_certificates(st, ctx)
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
    # Retryable infrastructure refusals still charge any admitted setup/probe time above, but they are
    # not completed seed evidence. Excluding them from the resume memo lets an unchanged seed retry after
    # GPU discovery, a Card re-pin, or the container runtime is repaired.
    # `isinstance` FIRST: this is a set membership, so it hashes the raw value, and an unhashable
    # reason (a list/dict on one confirm_eval row) raised TypeError out of the fold and bricked every
    # replay/resume of the run — the fold loop has no per-event try/except. The earlier `!= "aborted"`
    # comparisons are safe; this one needs the same shape guard as the rest of this handler's reads.
    _reason = d.get("reason")
    retryable_infrastructure = (isinstance(_reason, str)
                                and _reason in {"gpu_unavailable", "gpu_unpinnable"})
    if keyed and not retryable_infrastructure:               # per-seed resume memo (#0)
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
    # ALIASES the live `Event.data` (as do host_grading, leakage, archive, spec_proposed,
    # hypothesis_ranking, the `append(d)` audit journals, and Node.files/deleted). The fold runs on
    # EVERY loop iteration over the whole log, so copying each of these would be real per-iteration
    # cost for a hazard none of them carry: they are read-only PROJECTIONS — no consumer writes back
    # through them. `_on_fork`/`_on_inject_node` are the exception and DO copy, because those dicts
    # are REQUEST records the engine consumes, and `EventStore` caches parsed Events across
    # `read_all()`, so an in-place edit there would silently diverge every later fold in the process
    # from the bytes on disk. The rule for a new handler: alias a projection, copy a request.
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

def _on_run_setup_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # The command is ABOUT to run its arbitrary side effects. Until its finish lands, a resume cannot
    # tell "never started" from "died halfway through the install", so record the open attempt; the
    # finish below closes it. Old logs whose started row carried no `command` simply add nothing —
    # they fold exactly as before.
    # `run_setup_key` joins over the command, so a truthy SCALAR raised TypeError out of the fold.
    # An unusable shape is not a setup attempt we can key — treat it like the old logs that carried
    # no `command` at all and add nothing (fold must stay total).
    if isinstance(d.get("command"), (list, tuple)) and d.get("command"):
        st.run_setup_open.add(run_setup_key(d.get("command")))

def _on_run_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # arch-review §5 P2: a SUCCESSFUL run-level `run_setup` (dep install) is folded (keyed by its
    # command) so a resume skips it instead of re-installing every time — crash-safe exactly-once. A
    # failed/timed-out setup is NOT recorded (the command must actually re-run). Old logs whose
    # run_setup_finished carried no `command` just don't populate the set (setup runs as before).
    if not isinstance(d.get("command"), (list, tuple)) or not d.get("command"):
        return                       # same shape guard as the started handler above
    key = run_setup_key(d.get("command"))
    # ANY finish closes the open attempt — a failed/timed-out command reported its outcome, so the
    # next process is not resuming through an unknown one. Only exit 0 marks it done.
    st.run_setup_open.discard(key)
    if d.get("exit_code") == 0 and not d.get("timed_out"):
        st.run_setup_done.add(key)

def _on_data_leakage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.leakage = d

def _on_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Compare-and-set: the request carries the seq it believes it follows and is honoured only when it
    # lands exactly there, so a stale one cannot re-open the gate after an intervening abort/reset.
    # `isinstance(raw_after, bool)` is explicit because `isinstance(True, int)` is True — without it a
    # `after_seq=True` request placed at seq 2 would coerce to 1 and SATISFY the CAS. Pinned (both
    # directions, and that exact bool placement) by tests/test_events_replay.py::
    # test_a_stale_approval_request_is_rejected_and_a_current_one_is_not.
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
    # direct-best-neutral, not behaviorally inert: Strategist/proposal cues read it.
    st.coverage_snapshots.append(d)   # at_node/projection gates dedup and reject stale snapshots

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
        # How many of `calls` the provider actually stated an amount for (`core/llm.py::
        # cost_is_reported`). Plain sanitizer only — the reader-side default for a log written
        # before this field existed belongs to `_row_priced_calls`, which still has the raw row.
        "priced_calls": _llm_counter(raw.get("priced_calls")),
        "prompt_tokens": _llm_counter(raw.get("prompt_tokens")),
        "completion_tokens": _llm_counter(raw.get("completion_tokens")),
        "total_tokens": _llm_counter(raw.get("total_tokens")),
    })
    return out


def _row_priced_calls(raw: object, clean: dict) -> int:
    """Priced-call count for ONE usage/summary row, with the pre-counter reader-side default.

    `priced_calls` is additive (invariant 5), so every log written before it existed omits it, and
    the default chosen there decides what the UI says about ~every historical run. Neither constant
    works: 0 reports runs with a real invoice as unpriced, `calls` reports the unpriced ones as
    fully priced. The row settles it itself — a nonzero `cost` on that row IS the provider having
    stated an amount, and a zero one is exactly the evidence that it did not
    (`core/llm.py::cost_is_reported`). Measured against `runs/rubert-dr-0804`, whose gateway started
    reporting prices mid-run, this recovers the true 209-priced-of-313 split from the existing log
    with no migration; `runs/rubert-dr-0805` stays 0-of-354.

    A modern row always carries the field, so this branch cannot mislabel a new run.
    """
    if isinstance(raw, dict) and "priced_calls" in raw:
        return int(clean["priced_calls"])
    # Deliberately a COPY of `core/llm.py::inferred_priced_calls` rather than an import of it: that
    # module pulls in the openai/httpx transport (measured 0.5 s, ~4x this module's whole import) and
    # `fold` is on every state read. The rule is one comparison and its rationale lives at the shared
    # definition — change both together, and prefer the import if that weight ever goes away.
    return int(clean["calls"]) if float(clean["cost"]) > 0.0 else 0


def _on_run_concepts(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """PART V (B): set the RUN's BASE concept set (last-write-wins). Nodes may then author only deltas vs
    this base; the fold post-pass materializes their node_concepts. Additive; a malformed replacement
    keeps the previous membership but POISONS the integrity receipt (it is not silently ignored — that
    would certify stale taxonomy bytes as the current exact base)."""
    concepts = d.get("concepts")
    if isinstance(concepts, list):
        base, overflow, invalid = bounded_raw_concept_values(concepts)
        # keep the folded/FoldCursor base bounded; the append-only event remains the raw
        # audit source. Last valid run_concepts wins for both membership and its integrity receipt.
        st.run_base_concepts = list(dict.fromkeys(base))
        ctx.run_base_capped = overflow
        ctx.run_base_invalid = invalid
        ctx.run_base_seen = True
    elif concepts is not None and ctx.run_base_seen:
        # A non-list `concepts` is a REPLACEMENT the operator/agent intended and the fold cannot apply.
        # Dropping it silently left the PREVIOUS base standing behind its clean receipt, so the
        # ConceptFrame certified stale taxonomy bytes as the current exact base. Keep the old membership
        # (there is nothing valid to replace it with) but poison the receipt, which is what
        # CONCEPT_INVALID_ID_REASON already means downstream: "this base is not exact, don't trust it".
        # `run_base_seen` is deliberately NOT set here — a malformed row is not an inheritance source —
        # and this branch requires it, so a malformed row with NO established base still produces the
        # bare `delta_dependency_missing_run_base` receipt: already maximally degraded, and "invalid id"
        # would misdescribe a payload that carried no ids at all.
        # Unreachable via the sanctioned writers (serve/control_validation.py rejects a non-list with 400 and
        # engine/strategy.py always appends a list); this is the forged/hand-edited-log path the rest of
        # these integrity receipts exist for.
        ctx.run_base_invalid = True


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
    # receipts are derived from scratch on every full fold and FoldCursor snapshot. A
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
        # an absent EV_RUN_CONCEPTS is not an exact empty base. Order-tolerant logs may append
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
                # an explicit full-set producer may be low-trust display taxonomy and still
                # define inheritance (offline heuristic), but an unknown/future producer or missing
                # provenance is not an exact set. Classifier/operator/authored-full remain authoritative.
                if parent_provenance not in _INHERITABLE_CONCEPT_PROVENANCE:
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
                    or st.node_concept_provenance.get(parent_id)
                        not in _INHERITABLE_CONCEPT_PROVENANCE):
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


# The only fields any consumer reads off a coverage snapshot, with the shape each one is read AS:
# `at_node`/`projection_token` gate liveness (`snapshot_matches_analytics_projection`), `fired` +
# `directive` drive the pivot cue, `current_streak`/`recent_axis`/`locked_axis` drive
# capability-expansion, and the rest is display/diagnostic. Anything else on the row is dropped.
_COVERAGE_SNAPSHOT_STR = ("projection_token", "directive", "top_concept", "locked_axis",
                          "recent_axis", "tag_mode")
_COVERAGE_SNAPSHOT_INT = ("at_node", "experiments", "streak", "current_streak")
_COVERAGE_SNAPSHOT_FLOAT = ("top_concept_frac",)
_COVERAGE_SNAPSHOT_LIST = ("uncovered_key", "uncovered_axes")
_COVERAGE_TEXT_MAX = 2_000
_COVERAGE_LIST_MAX = 64


def _coverage_snapshot_row(d: dict) -> dict:
    """One coverage snapshot, re-bound into a detached allow-listed row with every field bounded.

    Absent/ill-typed fields are simply omitted, so a consumer's `.get(...)` sees the same "no signal"
    it already handles — never a str where it expects an int, and never an unbounded blob.
    """
    row: dict = {}
    for key in _COVERAGE_SNAPSHOT_STR:
        value = d.get(key)
        if isinstance(value, str):
            row[key] = value[:_COVERAGE_TEXT_MAX]
    for key in _COVERAGE_SNAPSHOT_INT:
        value = d.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            row[key] = value
    for key in _COVERAGE_SNAPSHOT_FLOAT:
        value = d.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
            row[key] = float(value)
    for key in _COVERAGE_SNAPSHOT_LIST:
        value = d.get(key)
        if isinstance(value, list):
            row[key] = [item[:_COVERAGE_TEXT_MAX] for item in value[:_COVERAGE_LIST_MAX]
                        if isinstance(item, str)]
    if isinstance(d.get("fired"), bool):
        row["fired"] = d["fired"]
    return row


def _on_concept_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV Phase 2a: the fold only retains the coverage / uncovered-region curve and never selects
    # from it; the live proposal path may later consume the record as a steering cue. at_node dedups resume.
    # This journal is behavioral, not audit-only: ``capability_expansion_due`` can rewrite
    # the proposal cue and stamp KIND_EXPAND. So the row is admitted through a DETACHED, ALLOW-LISTED,
    # BOUNDED projection, like the card handlers — it used to append `d` verbatim, which is the exact
    # "retaining raw event data" this comment forbids: `strategy.py` / `proposal_cues.py` read
    # arbitrary fields off each snapshot to steer proposals, so a malformed or oversized field on a
    # hand-edited or foreign log flowed straight through, aliased into RunState and deep-copied on
    # every FoldCursor snapshot.
    st.concept_coverage_snapshots.append(_coverage_snapshot_row(d))

def _fold_node_concept_envelope(st: RunState, ctx: "_FoldCtx", n: Node, d: dict, current) -> None:
    """Fold ONE `node_created`'s concept envelope into the membership sidecars.

    Split out of `_on_node_created` (doc 25 EV-08), which interleaved ~110 lines of concept-envelope
    POLICY into a node LIFECYCLE handler. This is a self-contained sub-machine: it decodes the raw
    receipts, discriminates delta/full/unsupported mode, canonicalizes the transitional 40a5a94 rows,
    decides receipt protection across CLASSIFIER/OPERATOR/OFFLINE provenance, and then writes four
    sidecar maps (`node_concepts`, `node_concept_provenance`, `node_concepts_at_vocab`,
    `node_concept_deltas`) plus four `_FoldCtx` sets. It lives beside `_on_node_concepts` and
    `_on_concept_tag_edited` so every concept-membership writer sits together.

    `current` is the node this event REPLACES (None on a first create). The caller captured it before
    rebinding `st.nodes[n.id]`, and the subject-equality test below needs that OLD idea — so it is a
    parameter rather than something this function can re-read.

    Called AFTER `st.nodes[n.id] = n`. That write used to sit in the MIDDLE of this block; nothing
    here reads `st.nodes`, so hoisting it above the call is behaviour-preserving, and it is what lets
    the block leave the lifecycle handler in one piece.
    """
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
        # forward compatibility belongs at the node boundary. Keep the experiment and its
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
        # 40a5a94 briefly wrote non-empty delta lists before the discriminator existed.
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
    # Researcher-AUTHORED concepts populate the compatible concept read model at creation, but the
    # provenance sidecar prevents an admission consumer from mistaking that self-authored taxonomy for
    # independent classifier evidence. A later `node_concepts` event overrides both, last-write-wins.
    if current is not None and not concept_subject_unchanged:
        # a replacement node_created is a new tagging subject even if a malformed writer
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
        # lifecycle generation != concept-subject generation. Preserve legacy replay
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
    # only classifier vocabulary receipts may delay the classifier refresh cadence.
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
    # tags). Advisory: taxonomy dedup/cadence consumers can use the folded tags to steer later board
    # consolidation, but they never directly re-rank evaluated nodes. Order-tolerant + idempotent +
    # malformed-safe.
    hid = d.get("hyp_id")
    if not hid:
        return
    concepts = d.get("concepts")
    st.hypothesis_concepts[str(hid)] = [str(c) for c in concepts] if isinstance(concepts, list) else []
    av = d.get("at_vocab")   # B1-ext: staleness reference (absent on pre-B1 events -> 0/oldest)
    if isinstance(av, int) and not isinstance(av, bool) and av >= 0:   # bool is an int subclass — reject
        st.hypothesis_concepts_at_vocab[str(hid)] = av
    else:
        # concepts and their vocabulary receipt are one LWW value. An older receipt would
        # make newly-derived tags look fresh and incorrectly suppress their next retag cadence.
        st.hypothesis_concepts_at_vocab.pop(str(hid), None)

def _on_concept_consolidation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV D5 B3 (§21.18): ACCUMULATE the consolidation rename map so decisions stay fixed across
    # cadences (stable vocabulary). It canonicalizes later membership/coverage inputs and can therefore
    # steer later proposals, without directly re-ranking evaluated nodes. Idempotent + malformed-safe.
    # ORDER-TOLERANT (invariant 5):
    # a CONFLICTING re-map of the same raw id (raw->a in one event, raw->b in another) resolves to a
    # DETERMINISTIC winner — the lexicographically smallest canonical — never last-write, so
    # fold(perm(events)) is byte-identical. The B3 producer fixes each decision once and never re-maps an
    # existing raw id, so a conflict only arises in an adversarial / spliced log; this just hardens it.
    # CODEX AGENT: lexicographic conflict resolution lets a later lower endpoint replace an already
    # durable consolidation decision and reinterpret historical coverage. Preserve first authority
    # (quarantining conflicts), or require an explicit versioned governance event for remapping.
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
    # Advisory: hierarchy/coverage projections can feed later strategy and proposal cues, but the edges
    # never directly re-rank evaluated nodes. Accepts a batch (`edges: [...]`) or one inline edge; a
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
        if "\t" in src or "\t" in rel or "\t" in dst:
            # The map key below tab-joins the triple; a component containing the delimiter would let two
            # DISTINCT triples collide on one key (e.g. ("a\tb","c","d") and ("a","b\tc","d")), and equal-
            # ranked colliders become order-dependent first-write-wins — breaking the commutative accumulate
            # this reducer claims (invariant 5 order-tolerance). A real concept id / relation / provenance
            # never contains a tab (ids are letters/digits/-._/), so a tab-bearing component is a
            # forged/malformed row: skip it like any other, keeping the key injective over the triple.
            continue
        if rel == "co_occurs":
            # this relation is a cache of current node membership, not an immutable
            # assertion. The old max-wins fold cannot express count decreases or deletion, so retaining
            # legacy rows creates permanent ghost edges. ConceptFrame derives it from the exact folded
            # membership snapshot; omit it here so large legacy caches cannot consume live edge budgets.
            continue
        conf = ed.get("confidence")
        # REVIEW(2026-07-16): the tuple order below can only rank a REAL finite float. Agent-supplied
        # values must be neutralized to keep the fold commutative (invariant 5 order-tolerance):
        #   * bool — isinstance(True, int) is True, so a stray `confidence: true` would coerce to 1.0 and
        #     could WIN over a legitimate edge; treat it as 0.0 (lowest) so a mis-typed flag never ranks.
        #   * NaN — every `>` comparison against a NaN tuple-head is False, so whichever edge arrived
        #     FIRST would stick forever ([nan, 5.0] keeps nan while [5.0, nan] keeps 5.0).
        #   * ±inf — a `+inf` head would permanently outrank every finite repair while `ConceptFrame`
        #     drops the same edge (`finite_metric` returns None -> rejected at serve/concept_frame.py:352),
        #     so replay and the UI read would disagree with no way to converge.
        # NaN/±inf are unreachable over the event log TODAY — it is orjson end to end: `orjson.dumps`
        # writes a non-finite float as `null` (-> the isinstance guard below yields 0.0) and
        # `orjson.loads` REJECTS the `NaN`/`Infinity` literals (and any `1e400`-style overflow) that
        # stdlib json would accept, so such a row ends the recoverable prefix instead of folding.
        # Normalizing anyway is free and keeps the total order a property of THIS function rather than
        # of the transport, so swapping a parser can never silently reopen the hole.
        conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
        if not math.isfinite(conf):
            conf = 0.0
        # -0.0 and 0.0 tie numerically but serialize differently. Canonicalize the sign
        # before the commutative max so replay order cannot leak into RunState / ConceptFrame bytes.
        if conf == 0.0:
            conf = 0.0
        prov = str(ed.get("provenance") or "")
        # The tab join is now injective over the triple: any component containing the delimiter was
        # rejected above, so distinct triples can never collide on one key and the accumulate below stays
        # commutative (invariant 5 order-tolerance).
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
        st.llm_cost["priced_calls"] = _row_priced_calls(d, st.llm_cost)


def _on_llm_usage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    usage_id = d.get("usage_id")
    if isinstance(usage_id, str) and usage_id:
        if usage_id in ctx.llm_usage_ids:
            ctx.llm_usage_seen = True
            return
        ctx.llm_usage_ids.add(usage_id)
    base = _clean_llm_totals(st.llm_cost)
    delta = _clean_llm_totals(d)
    delta["priced_calls"] = _row_priced_calls(d, delta)
    base["cost"] = min(_MAX_LLM_COST, float(base["cost"]) + float(delta["cost"]))
    for key in ("calls", "priced_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
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
    # A7 Strategist behavioral replay state: rebuild the chosen Strategy without re-calling the LLM;
    # engine re-entry applies active_strategy before the next decision/evaluation boundary.
    st.active_strategy = d.get("strategy")
    history = {"strategy": d.get("strategy"), "at_node": d.get("at_node"),
               "ctx": d.get("ctx")}
    if d.get("developer_application") is not None:
        history["developer_application"] = d["developer_application"]
    st.strategy_history.append(history)

def _on_hypothesis_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT board prioritization: latest wins. The order does not re-rank evaluated nodes; the sole
    # board derivation `_derive_cards` uses it to stamp Card.priority (the compatibility priority
    # fallback when no native card_ranked receipt exists).
    n = _node_for_event(st, d)
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    # `_derive_cards` iterates `(...).get("order") or []` unguarded, so a truthy SCALAR `order`
    # raised TypeError out of the fold and bricked the run. The native twin `_on_card_ranked`
    # already bounds this and says why: "a malformed/future order is an honest empty ranking, never
    # an iterable assumption that can brick replay". Same treatment here.
    raw_order = d.get("order")
    st.hypothesis_ranking = ({**d, "order": raw_order} if isinstance(raw_order, list)
                             else {**d, "order": []})

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
    # `signals` MUST be a list of dicts. `hard_flagged_ids._has_current_hard_signal` later runs
    # `s.get("signal", "")` over `rh.get("signals") or []`, and the fold loop has no per-event
    # try/except — so a forged/hand-edited truthy SCALAR (`"leak"`, `5` -> TypeError on iteration)
    # or a list of non-dicts (`["leak"]`, `[None]` -> AttributeError on `.get`) bricks EVERY
    # fold/replay/resume/view of the run under trust_gate gate/block, and the digest's trust
    # reflection even under `audit`. Same class as the `node_ids` scalar guard above; fold must stay
    # total. Bounded like every other list this fold admits, so a forged event cannot park an
    # unbounded array in RunState either.
    raw_signals = d.get("signals")
    record = {"node_id": nid,
              "signals": [s for s in raw_signals if isinstance(s, dict)][:64]
                         if isinstance(raw_signals, list) else [],
              "evidence_version": d.get("evidence_version", 0),
              "code_digest": d.get("code_digest")}
    if n is not None:
        record["generation"] = n.attempt
    st.reward_hacks.append(record)

def _on_foresight_selected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT receipt: does not re-rank evaluated nodes, but primes later world-model picks with
    # its OWN calibration (did the picked node beat its parent?), closing the predict→outcome loop.
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
    # P1+: engine-written agentic merge — fold alias beliefs into a canonical. Collected
    # here, APPLIED deterministically in `_derive_cards` (no LLM in the fold). A malformed
    # entry is tolerated there; unknown on old logs -> skipped by the outer dispatch.
    receipt = _bounded_card_merge_receipt(d)
    if receipt is not None:
        receipt["_event_index"] = ctx.event_index
        st.hypotheses_merged.append(receipt)

def _on_hypothesis_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1: an explicitly-registered hypothesis (human `add_hypothesis`, or a deep-research
    # direction) — may have no evidence yet. Evidence + verdict are DERIVED post-loop.
    statement = d.get("statement")
    clean_statement = statement.strip() if isinstance(statement, str) else ""
    try:
        statement_bytes = len(clean_statement.encode("utf-8"))
    except UnicodeError:
        statement_bytes = _CARD_REPLAY_STATEMENT_MAX_BYTES + 1
    if (clean_statement and len(clean_statement) <= _CARD_REPLAY_STATEMENT_MAX
            and statement_bytes <= _CARD_REPLAY_STATEMENT_MAX_BYTES):
        receipt = {"statement": clean_statement}
        for key, limit in (("id", 256), ("source", 64), ("rationale", 400)):
            value = d.get(key)
            if isinstance(value, str) and value.strip() and len(value.strip()) <= limit:
                receipt[key] = value.strip()
        at_node = d.get("at_node")
        if type(at_node) is int and 0 <= at_node <= (1 << 31) - 1:
            receipt["at_node"] = at_node
        st.hypotheses_added.append(receipt)
        # Re-adding an abandoned statement reopens it (last write wins).
        try:
            hid = str(receipt.get("id") or hypothesis_id(receipt["statement"]))
            if hid in st.hypotheses_abandoned:
                st.hypotheses_abandoned.remove(hid)
        except Exception:
            pass



_GPU_INVENTORY_ROW_KEYS_MAX = 32          # the real schema has 7; this only stops a fat foreign row
_GPU_INVENTORY_SCALAR_CHARS_MAX = 256     # uuid / pci_bus_id / name / driver strings are far shorter


def _bounded_gpu_inventory_row(row: dict) -> dict:
    """Copy one GPU-inventory row with both its key count and its scalar sizes bounded."""
    out: dict = {}
    for key in sorted(row):
        if len(out) >= _GPU_INVENTORY_ROW_KEYS_MAX:
            break
        if not isinstance(key, str) or len(key) > _GPU_INVENTORY_SCALAR_CHARS_MAX:
            continue
        value = row[key]
        if isinstance(value, str) and len(value) > _GPU_INVENTORY_SCALAR_CHARS_MAX:
            continue                      # dropped, not truncated — a shortened uuid is another GPU
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            continue                      # nested containers are unbounded by construction
        out[key] = value
    return out




def _on_card_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Hypothesis-card Kanban (docs/23): bounded registration plus an optional immutable ownership
    # receipt. Unreceipted historical rows remain visible shadows; only `_derive_cards` may validate
    # native identity/readiness. Evidence/verdict/status are derived.
    receipt = _bounded_card_added_receipt(d)
    if receipt is not None:
        # RunState is deep-copied on every incremental snapshot. Never retain Event.data
        # here: one unknown megabyte field would otherwise be multiplied by every live state read.
        st.cards_added.append(receipt)

def _on_card_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Engine-written agentic merge — fold alias cards into a canonical. Collected here, APPLIED
    # deterministically in `_derive_cards` (no LLM in the fold), order-tolerant, back-compat on old logs.
    receipt = _bounded_card_merge_receipt(d)
    if receipt is not None:
        # aliases are identity-bearing, so cap the durable prefix before RunState owns it;
        # unknown merge metadata has no replay semantics and remains only in the append-only log.
        receipt["_event_index"] = ctx.event_index
        st.cards_merged.append(receipt)

def _on_card_dropped(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Canonical engine effect (`card_auto_dropped`) or operator intent (`card_dropped`):
    # {id, reason, dropped_by}. Historical engine-authored `card_dropped` rows intentionally share this
    # handler so old logs retain byte-for-byte replay semantics after the event namespace split.
    receipt = _bounded_card_drop_receipt(d)
    if receipt is not None:
        # keep a typed lifecycle receipt, not the raw control payload. This also prevents
        # arbitrary objects from becoming enormous strings later in `_derive_cards`.
        receipt["_event_index"] = ctx.event_index
        st.cards_dropped.append(receipt)


def _on_card_reprioritized(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one server-stamped operator priority pin into its last-write-wins map."""
    card_id = _card_replay_id(d.get("id"))
    priority = d.get("priority")
    if (card_id is not None and d.get("source") == "operator" and d.get("pinned") is True
            and type(priority) is int and 0 <= priority < 256):
        # Reinsert so dict iteration preserves GLOBAL last-event order even when aliases later merge
        # several raw ids onto one canonical Card. Plain assignment would retain first-insertion order.
        st.card_priority_pins.pop(card_id, None)
        st.card_priority_pins[card_id] = priority


def _on_card_edited(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold a display-only edit; the immutable seed/action receipt remains untouched."""
    card_id = _card_replay_id(d.get("id"))
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if card_id is not None and statement is not None and d.get("source") == "operator":
        st.card_operator_edits.pop(card_id, None)
        edit = {"statement": statement, "source": "operator"}
        # Legacy/in-memory Event objects may not carry a durable sequence. Never manufacture an
        # acknowledgement for those rows; modern EventStore records always take this exact branch.
        if type(e.seq) is int and e.seq >= 0:
            edit["event_seq"] = e.seq
        st.card_operator_edits[card_id] = edit


def _on_card_resource_pinned(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold a quantitative operator override without rewriting receipt-owned ``footprint``."""
    card_id = _card_replay_id(d.get("id"))
    if card_id is None or d.get("source") != "operator" or d.get("pinned") is not True:
        return
    pin: dict[str, int | str] = {"pinned_by": "operator"}
    for key in ("gpus", "gpu_mem_mib"):
        value = d.get(key)
        if type(value) is int and 0 <= value <= _CARD_REPLAY_NODE_ID_MAX:
            pin[key] = value
        elif key in d:
            return
    if len(pin) > 1:
        st.card_resource_pins.pop(card_id, None)
        st.card_resource_pins[card_id] = pin

def _on_card_enriched(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Layer 1b: a delta onto a card (novelty verdict, cross-run prior, footprint-finalize, steering cues).
    # Collected here; APPLIED last-write-by-envelope-order in `_derive_cards`.
    raw_id = d.get("id")
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id.strip()) <= 256:
        rec = {"id": raw_id.strip()}
        fence_keys = {"node_id", "generation", "proposal_ref"}
        modern = bool(fence_keys & set(d))
        if modern:
            # Current engine deltas are bound to the exact Node lifecycle and proposal. A partial or
            # malformed fence is not a legacy row; dropping it prevents an enrichment from following a
            # numeric node slot after reset/re-proposal.
            node_id = d.get("node_id")
            generation = d.get("generation")
            proposal_ref = d.get("proposal_ref")
            digest = proposal_ref.get("digest") if isinstance(proposal_ref, dict) else None
            if (type(node_id) is not int or not 0 <= node_id <= (1 << 31) - 1
                    or type(generation) is not int or not 0 <= generation <= (1 << 31) - 1
                    or not isinstance(proposal_ref, dict)
                    or set(proposal_ref) != {"v", "digest"} or proposal_ref.get("v") != 1
                    or not valid_digest_ref(digest, prefix="idea:v1:")):
                return
            rec.update({
                "node_id": node_id,
                "generation": generation,
                "proposal_ref": {"v": 1, "digest": digest},
            })
        allowed = (
            "novelty_verdict", "cross_run_prior", "footprint", "steering_context",
            "concept_tags", "lesson_refs", "claim_refs", "research_origin",
            "foresight_rank", "confidence",
        )
        # bound each allow-listed sibling independently. A huge lexically-early unknown
        # field must not consume a shared budget and erase id or a later valid field.
        for key in allowed:
            if key not in d:
                continue
            if key == "concept_tags":
                # keep enough derived receipt data to say that a node-less enrichment was
                # lossy.  The caller-provided provenance_tier is intentionally not copied: a free-form
                # delta must never promote its own tags to classifier/operator truth.
                if not isinstance(d[key], list):
                    continue
                values, overflow, invalid = bounded_raw_concept_values(d[key])
                rec[key] = values
                rec["_concept_tags_overflow"] = overflow
                rec["_concept_tags_invalid"] = invalid
                continue
            value = d[key]
            if key == "steering_context":
                bounded = normalize_steering_context(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "footprint":
                bounded = _bounded_card_footprint_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "novelty_verdict":
                bounded = _bounded_card_novelty_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "cross_run_prior":
                bounded = _bounded_card_cross_run_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "research_origin":
                bounded = _bounded_card_ref(value)
                if bounded is not None and (not modern or _digest_ref(bounded, "memo")):
                    rec[key] = bounded
                continue
            if key in {"lesson_refs", "claim_refs"}:
                if not isinstance(value, list):
                    continue
                namespace = "lesson" if key == "lesson_refs" else "claim"
                refs: list[str] = []
                for item in value[:64]:
                    bounded = _bounded_card_ref(item)
                    if (bounded is not None and bounded not in refs
                            and (not modern or _digest_ref(bounded, namespace))):
                        refs.append(bounded)
                rec[key] = refs
                continue
            valid, bounded = _bounded_card_enrichment(value)
            if valid:
                rec[key] = bounded
        # envelope seq is authoritative; physical order is the deterministic tie-break for
        # legacy/default envelopes. Assign both after copying so payload fields can never spoof ordering.
        rec["_seq"] = e.seq if type(e.seq) is int else -1
        rec["_event_index"] = ctx.event_index if type(ctx.event_index) is int else -1
        # Keep one LWW candidate per raw Card id, exact lifecycle fence, and semantic field. Full
        # history remains in events.jsonl; RunState/FoldCursor retain only projection candidates.
        identity_keys = {"id", "node_id", "generation", "proposal_ref", "_seq", "_event_index"}
        semantic_keys = [key for key in rec if key not in identity_keys and not key.startswith("_concept_tags_")]
        fence = (
            rec["id"], rec.get("node_id"), rec.get("generation"),
            (rec.get("proposal_ref") or {}).get("digest"),
        )
        order = (rec["_seq"], rec["_event_index"])
        for key in semantic_keys:
            candidate = {name: rec[name] for name in identity_keys if name in rec}
            candidate[key] = rec[key]
            if key == "concept_tags":
                for flag in ("_concept_tags_overflow", "_concept_tags_invalid"):
                    if flag in rec:
                        candidate[flag] = rec[flag]
            candidate_key = (*fence, key)
            replace_at = ctx.card_enrichment_index.get(candidate_key)
            if replace_at is not None:
                prior = st.cards_enriched[replace_at]
                prior_order = (
                    prior.get("_seq") if type(prior.get("_seq")) is int else -1,
                    prior.get("_event_index")
                    if type(prior.get("_event_index")) is int else -1,
                )
                if order >= prior_order:
                    st.cards_enriched[replace_at] = candidate
            elif len(st.cards_enriched) < CARD_ENRICHMENT_JOURNAL_MAX:
                ctx.card_enrichment_index[candidate_key] = len(st.cards_enriched)
                st.cards_enriched.append(candidate)
            else:
                # Handler-level LWW means repeated values for the same rejected window are still one
                # missing projection candidate, not an ever-growing count of audit-log events.
                ctx.card_enrichment_omissions.setdefault(candidate_key, 1)

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
    # a malformed/future order is an honest empty ranking, never an iterable assumption
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
    # Drop dangling markers on normal completion. Error finishes deliberately retain crash prefixes:
    # older/external writers may need resume recovery to append the missing node_failed receipt. Other
    # terminal reasons must not leave a false in-flight pulse on a run that is over.
    if d.get("reason") != "error":
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
        # A reopen begins a new candidate epoch, so the prior epoch's confirmation certificate must
        # not keep authorizing selection. Clearing only the folded flag and not the threaded
        # `ctx.best_confirmed` here is what let an epoch-(N-1) certificate keep overriding epoch-N's
        # metric winner — the bug this shared helper now makes unreachable from any one site.
        _invalidate_completion_certificates(st, ctx)
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
        _purge_node_requests(st, {nid})
        if st.approval_subject == nid or st.approved_node_id == nid:
            # A FINISHED run keeps its certificates; only the grant that named THIS node is void.
            _clear_approval(st)
        if st.champion == nid:
            st.champion = None
        if st.finished:
            if ctx.best_confirmed == nid:
                ctx.best_confirmed = None
            return
        _invalidate_completion_certificates(st, ctx)
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
        # malformed historical control events must remain total under replay. Reject
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
            # one folded key per authority family preserves true event-order LWW while
            # retaining the latest event's spelling for old/no-broker resume compatibility.
            st.budget_overrides.pop(
                _legacy if _key == _canonical else _canonical, None)
            st.budget_overrides[_key] = _value
            if _canonical == "llm_parallel" and _key == _canonical:
                # the legacy alias historically governed only build fan-out. Preserve the
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
    elif not any(
        standing_hint_dedup_key(hint) == standing_hint_dedup_key(d)
        for hint in st.pending_hints
        if isinstance(hint, dict)
    ):
        # Standing directives are semantic state, not command history. A double click, lost-response
        # retry, or old duplicate events must not repeat the same instruction in every later prompt.
        st.pending_hints.append(d)

def _on_set_strategy(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
    # engine applies it before consulting the Strategist, so a human always wins. The pin owns
    # only the fields it names (including canonical concurrency/lane allocations) and STAYS in force
    # for the rest
    # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
    # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
    st.pending_strategy = d.get("strategy")

def _purge_node_requests(st: RunState, drop) -> None:
    """Drop queued force-confirm / force-ablate intents naming any node in `drop` (doc 25 EV-09).

    FOUR lists, in two pairs, and each pair must move together: a legacy bare-id list and a
    generation-stamped record list. Filtering one and forgetting its twin is the failure this
    single-sources, and it is not symmetric — the engine's `_pending_forced_*` readers consult the
    STAMPED list first, so a record left behind wins over an id the caller believed it had removed,
    and the request fires against a lifecycle that no longer exists.

    All four call sites — requeue-on-partition, tombstone, reset and abort — drop both queues. They
    are the events that end a node's current lifecycle, and a queued intent names a lifecycle, not
    a node.
    """
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in drop]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in drop]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in drop]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in drop]


def _queue_forced_request(st: RunState, d: dict, requests: list, generations: list) -> None:
    """Fold a `force_confirm` / `force_ablate` intent onto its queue pair.

    The two handlers were byte-identical but for the target lists. The shape is a generation CAS:
    a stamped intent is queued only when the node is live and its `attempt` still matches, so a
    control authored against a since-reset lifecycle is DROPPED rather than applied to new code.
    The legacy arm exists for logs predating the stamp — an unstamped intent for a node that has not
    been created yet binds when it appears, which is why it is admitted with no generation check.
    """
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        requests.append(nid)
        generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        requests.append(nid)   # legacy queued-before-create intent


def _on_force_confirm(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _queue_forced_request(st, d, st.confirm_requests, st.confirm_request_generations)

def _on_force_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _queue_forced_request(st, d, st.ablate_requests, st.ablate_request_generations)

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

def _advance_request_cursor(done: int, total: int, idx: object) -> int:
    """The ONE rule every `<x>_requests` / `<x>s_done` positional gate advances by.

    `fork_requests`/`forks_done` and `inject_requests`/`injects_done` are the operator-steering
    queues: the engine serves `requests[done]` and appends a receipt naming the position it just
    completed. Both used to hand-roll their own partial version of this, with complementary holes —
    fork keyed on `from_node_id` (not unique: re-forking one promising node is the ordinary pattern,
    so a duplicate receipt consumed the operator's SECOND fork), inject keyed on a stamped absolute
    index with no queue bound (an orphan receipt walked the cursor past the queue and stranded the
    next intent forever). `_on_card_build_done` already had the right shape; this is that shape,
    shared, so a third queue cannot invent a fourth set of semantics.

    The receipt names its own position, which makes the rule self-healing rather than cumulative:

    * a receipt for a position BEFORE the cursor is one already completed — a duplicate or a replay —
      and must not consume the request now at the head;
    * a receipt at or after the cursor completes through that position, clamped to the queue, so a
      log whose stamped indices were computed under older fold semantics still converges on
      "everything up to here was served" instead of silently dropping every later receipt;
    * the cursor may never overrun the queue, so nothing can strand an intent appended afterwards.

    A row with NO usable index is legacy (or forged): it advances by one, still queue-bounded, so old
    logs fold exactly as they always did. Bools are rejected — `type(True) is int` is False, but an
    explicit check keeps that a stated property rather than an accident of the type test.
    """
    total = max(0, total)
    done = min(max(0, done), total)
    if isinstance(idx, bool) or type(idx) is not int:
        return min(done + 1, total)          # legacy/unusable index — advance one, never past the end
    if idx < done:
        return done                          # already completed; not this head's receipt
    return min(idx + 1, total)


def _on_fork_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Advance the fork queue cursor by the position the receipt names.

    Every producer serves `fork_requests[forks_done]` and stamps that index (`_serve_forced_requests`
    and `_close_node_creating_forced_request_before_terminal_gate`). `from_node_id` is deliberately
    NOT the key: two queued forks of the same parent are indistinguishable by it, which is exactly the
    case a duplicate receipt used to consume. It stays a bind for LEGACY rows that carry no index, and
    `generation` is compared by neither — the served branch stamps `current.attempt`, which for a
    legacy unstamped request differs from the request record by design.
    """
    if "idx" not in d:
        # Legacy receipt: keep the parent bind that shipped before the index existed. It cannot
        # separate two same-parent forks, but it still rejects a receipt naming a different fork.
        if st.forks_done < len(st.fork_requests):
            head = st.fork_requests[st.forks_done]
            receipt_pid = _coerce_node_id(d, "from_node_id")
            head_pid = _coerce_node_id(head, "from_node_id")
            if receipt_pid is not None and head_pid is not None and receipt_pid != head_pid:
                return                       # a receipt for some other fork cannot consume this head
    st.forks_done = _advance_request_cursor(
        st.forks_done, len(st.fork_requests), d.get("idx"))


def _on_card_build_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one main-task speculative build election.

    The request generation is the search epoch observed under ``_id_lock``. A late producer from an
    earlier epoch may still be given up by a matching done record, but a request itself cannot enter
    the queue with a stale/forged epoch. The compact normalized record is the durable buffer key.
    """
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    if (card_id is None or type(generation) is not int
            or not 0 <= generation <= _CARD_REPLAY_NODE_ID_MAX
            or generation != st.search_epoch):
        return
    st.card_build_requests.append({"card_id": card_id, "generation": generation})


def _on_card_build_attempted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one PHYSICAL producer attempt against a request head.

    Selection-neutral by construction: nothing here touches nodes, cards or the request queue — the
    row exists so a resume can see that a provider call for this exact request identity may already
    have been paid for. Unlike the request above it is NOT epoch-filtered: an attempt from a since-
    superseded epoch is still an attempt that may have been billed, and dropping it would erase the
    very evidence this record exists to keep.

    `index` is the queue POSITION the attempt was made against, the same discipline `card_build_done`
    uses. Without it a card that was closed and later re-elected at the same epoch would carry the
    identical (card_id, generation) key, and the old — already reconciled — attempt would quarantine
    a brand-new head forever. The fold stays dumb: it records the position, the engine decides whether
    an attempt still belongs to the open head.
    """
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    index = d.get("index")
    if (card_id is None or type(generation) is not int
            or not 0 <= generation <= _CARD_REPLAY_NODE_ID_MAX
            or type(index) is not int or index < 0):
        return
    st.card_build_attempts.append(
        {"card_id": card_id, "generation": generation, "index": index})


def _on_speculation_depth_settled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Adopt one AUTO re-resolution of the run's speculation depth.

    A MINIMUM, not an assignment, and that is the whole design. The engine only ever settles AUTO
    DOWNWARD (`orchestrator.py::_settle_speculation_depth`), so taking the minimum over every row
    reproduces the engine's own sequence while being ORDER-TOLERANT and IDEMPOTENT — invariant #5's
    two requirements. Last-write-wins would satisfy neither: two rows spliced in the other order, or
    one row folded twice, would land on a different treatment, and a duplicated stale row could raise
    a depth back up after the run had already narrowed it.

    THE MINIMUM IS TAKEN OVER SETTLE ROWS ONLY, into a field of their own, and the effective depth is
    derived from that floor and the launch pin together (`_settle_folded_speculation_depth`). Folding
    the two facts into ONE field made the "order-tolerant" claim above false against the one event
    whose order actually mattered: `_on_run_started` ASSIGNS, so a settle row spliced BEFORE it was
    overwritten and the fold landed on the pin (measured on this exact log: 4 at splice position 0, 0
    at every other position). It was latent — `run_started` is first by construction — but this
    codebase writes ordering PRECONDITIONS down rather than leaving them as properties of an event
    (invariant #1 does it for `EV_NODE_EVAL_STARTED`), and here the precondition could simply be
    removed instead. There is now no order requirement between these two handlers at all.

    Nothing here is re-measured. The row's `evidence` is recorded for the operator and for
    `looplab inspect`; the fold reads only `depth`, so a resume on a box with different hardware
    continues under the treatment THIS RUN chose rather than one re-derived from the new host — the
    same property `run_started`'s pinned widths give, extended to a value that is allowed to move.

    Bounds are strict for the same reason `run_started`'s are: a bool/float/string in a malformed or
    hand-edited row must not be able to change the search treatment. A row the engine never wrote
    (depth above the pinned one) is simply inert, because the derivation caps the floor at the pin.
    """
    depth = d.get("depth")
    if type(depth) is not int or not 0 <= depth <= 64:
        return
    floor = st.speculation_depth_settled
    st.speculation_depth_settled = depth if floor is None else min(floor, depth)
    _settle_folded_speculation_depth(st)


def _on_run_width_settled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Adopt one proposal-derived RE-PIN of the run's concurrency widths (docs/29 F1).

    LAST WRITE WINS, into fields of this row's own, and both halves of that matter.

    *Into its own fields*, never onto `run_started`'s `eval_parallel`/`llm_parallel`, is what makes
    the pair ORDER-TOLERANT against `run_started` itself (invariant #5). `_on_run_started` ASSIGNS, so
    a repin folded ahead of it would be silently overwritten and the fold would land on the pin — the
    exact defect measured for the `speculation_depth` pair (4 at splice position 0, 0 everywhere
    else), written down there rather than left to be rediscovered here. Each field has one writer and
    neither handler reads the other's before writing its own; `Engine._repin_settled_widths` resolves
    them, and it is the only place that has to know the precedence.

    *Last write wins*, not the minimum `_on_speculation_depth_settled` takes, because this repin is
    genuinely TWO-WAY. The depth ratchet only ever narrows, so a minimum reproduced the engine's own
    sequence for free. A width follows what the research proposed: it narrows when the Cards declare
    wide footprints and widens back — never past the launch pin — when they stop. A minimum would turn
    one wide proposal into a permanent serialization of the run, and a maximum would make a
    hand-edited row able to widen a treatment the engine had already narrowed. LWW over a total log
    order is what the engine actually did, and it is what a resume has to reproduce.

    Nothing here is re-derived. The row's `evidence` records the pool, the demand and the widest
    declared footprint the decision was made from, for `looplab inspect` and the operator; the fold
    reads only the two integers, so a resume on a box with a different GPU count continues at the
    width THIS RUN chose rather than at one recomputed from the new host. That is the whole reason the
    decision needs a durable event and not a per-turn recomputation.

    Bounds are strict, and the floor is 1 rather than 0 for a reason `settle_width` states: a live
    `0` is not AUTO, and a repin row is a live value by definition. A malformed or out-of-range field
    is DROPPED INDEPENDENTLY of its sibling — one poisoned axis in a hand-edited row must not also
    discard a valid re-pin of the other, and dropping leaves that axis at whatever the previous rows
    and the `run_started` pin already established.
    """
    for key, upper in (("eval_parallel", 1024), ("llm_parallel", 64)):
        value = d.get(key)
        if type(value) is not int or not 1 <= value <= upper:
            continue
        setattr(st, f"{key}_settled", value)


def _on_card_build_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Advance exactly one positional Card-build request and retain its successful node link.

    A valid skipped result deliberately advances the counter so producer failure cannot wedge every
    resume. Unlike the older fork pair, the async producer can race a search-epoch change, so the writer
    must duplicate the exact request identity and replay advances only the matching current head.
    Orphan/malformed/mismatched done rows are inert and can never skip a later real request.
    """
    request_index = st.card_builds_done
    request = (st.card_build_requests[request_index]
               if request_index < len(st.card_build_requests) else None)
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    if (request is None or card_id != request["card_id"]
            or type(generation) is not int or generation != request["generation"]):
        return
    skipped = d.get("skipped")
    # `isinstance` FIRST: set membership hashes the raw event value, so a forged/corrupt `skipped`
    # that is a list/dict raised TypeError out of the fold and bricked every replay/resume of the run
    # (the fold loop has no per-event try/except). Every other field this handler reads is
    # shape-guarded; this one is now too.
    if isinstance(skipped, str) and skipped in {"producer_failed", "stale"}:
        st.card_builds_done += 1
        st.card_build_outcomes.append(skipped)
        if skipped == "producer_failed" and card_id not in st.card_build_producer_failed:
            st.card_build_producer_failed.append(card_id)
        return
    if skipped is not None or d.get("speculative") is not True:
        return
    node_id = _card_replay_node_id(d.get("node_id"))
    if node_id is None:
        return
    node = st.nodes.get(node_id)
    if (node is None or node.idea.card_id != card_id
            or getattr(node, "speculative", False) is not True
            or getattr(node, "card_build_generation", None) != generation):
        return
    # Keep only the exact bounded receipt consumed by depth/freshness recovery. Last write for a node
    # is harmless and deterministic; first-terminal lifecycle rules still own its actual node state.
    st.card_builds_done += 1
    st.card_build_outcomes.append("committed")
    st.speculative_nodes[node_id] = dict(request)

def _on_inject_node(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # COPY, like `_on_fork` does: `EventStore` caches parsed `Event`s across `read_all()`, so
    # storing the live `data` dict would let any in-place mutation of a folded request change
    # what every later fold in this process sees — folded state silently diverging from the
    # bytes on disk. No consumer mutates today; the copy keeps it that way by construction.
    st.inject_requests.append(dict(d))  # operator-authored experiment (manual tree edit)

def _on_inject_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Same positional rule as `_on_fork_done` — see `_advance_request_cursor` for why the receipt's
    # own index, not the fold's current cursor, is the authority. Both producers stamp
    # `{"idx": state.injects_done}`; a legacy row without one advances by a queue-bounded step.
    st.injects_done = _advance_request_cursor(
        st.injects_done, len(st.inject_requests), d.get("idx"))

def _on_deep_research(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.research_requests.append(d)       # manual "go think hard" request (control event)

def _on_research_attempted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Paid-attempt receipt appended BEFORE the Deep-Research provider call. Selection-neutral: only
    # the research trigger gates read it, exactly like the memo row below. Ignore a row without a
    # usable identity — an attempt nothing can ever reconcile would strand the manual queue.
    attempt_id = d.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return
    at_node = d.get("at_node")
    st.research_attempts.append({
        "attempt_id": attempt_id,
        "trigger": str(d.get("trigger") or ""),
        "at_node": at_node if type(at_node) is int and at_node >= 0 else None,
        "manual": bool(d.get("manual")),
    })

def _on_research_completed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Deep-Research memo: never re-ranks current nodes/best; later proposal context and cross-run
    # evidence may read it. `served_manual` also prevents replay from re-serving the request.
    from looplab.core.advisory_payloads import sanitize_research_memo_payload
    # old events predate D8 omission receipts. Preserve their replay shape (and unknown authority)
    # instead of manufacturing a complete receipt from an already-truncated legacy projection.
    st.research.append(sanitize_research_memo_payload(d.get("memo") or d, add_receipts=False))
    # `research_served` indexes `research_requests`: the engine only sets `served_manual` while
    # serving `research_requests[research_served]` (engine/research_cadence.py:60). Counting every
    # such row unconditionally let a duplicate/orphan completion push the cursor PAST the queue, so a
    # `deep_research` request appended afterwards sat at an index the manual trigger would never reach
    # — the operator's "go think hard now" was silently dropped. Clamping to the queue is a no-op on
    # any log a sanctioned producer wrote. There is no per-request identity to compare (the memo
    # carries none), so the head clamp IS the bind here.
    if d.get("served_manual") and st.research_served < len(st.research_requests):
        st.research_served += 1
    # Close this memo's paid attempt so the trigger gates stop counting it as still outstanding.
    # Order-tolerant: the attempt row may be folded before or after this one — the engine only ever
    # asks "which attempt ids are completed", never "in what order".
    attempt_id = d.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        st.research_attempts_completed.add(attempt_id)

def _on_lessons_distilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # M6 does not re-rank current nodes/best; at_node + pair ids are behavioral replay gates that
    # prevent paid re-distillation, while the shared lesson output can steer later proposals.
    st.lessons_distilled.append(d)

def _on_lessons_refreshed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.lessons_refreshed.append(d)   # M6 shared-store re-read cadence/replay gate

def _on_report_generated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Agent-authored run report (selection-neutral; NEVER touches nodes/best). Latest wins; the cadence
    # and manual-refresh paths both append this, and the receipt also gates future regeneration.
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
    content["published_at"] = event_timestamp(e)   # the shared rule; see `event_timestamp`
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
    EV_NODE_EVAL_STARTED: _on_node_eval_started,
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
    EV_RUN_SETUP_STARTED: _on_run_setup_started,
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
    EV_CARD_BUILD_REQUESTED: _on_card_build_requested,
    EV_CARD_BUILD_ATTEMPTED: _on_card_build_attempted,
    EV_SPECULATION_DEPTH_SETTLED: _on_speculation_depth_settled,
    EV_RUN_WIDTH_SETTLED: _on_run_width_settled,
    EV_CARD_BUILD_DONE: _on_card_build_done,
    EV_CARD_MERGED: _on_card_merged,
    EV_CARD_AUTO_DROPPED: _on_card_dropped,
    EV_CARD_DROPPED: _on_card_dropped,
    EV_CARD_REPRIORITIZED: _on_card_reprioritized,
    EV_CARD_EDITED: _on_card_edited,
    EV_CARD_RESOURCE_PINNED: _on_card_resource_pinned,
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
    EV_RESEARCH_ATTEMPTED: _on_research_attempted,
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

    _derive_cards(st, card_enrichment_omissions=ctx.card_enrichment_omissions)
    # docs/23 Layer 1a: the card ledger (mirrors hypotheses); advisory, after best
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
        # never finalize the accumulator itself. Several post-passes are destructive
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
    # multi-seed means + a significance test), so it overrides the mean pick. R1-d COMPOSITION:
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
