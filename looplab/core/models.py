"""Domain models + event envelope (I0). Pydantic v2; JSON Schemas derive from these."""
from __future__ import annotations

import math
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import (BaseModel, ConfigDict, Field, field_serializer, field_validator,
                      model_serializer, model_validator)

from looplab.core.cards import (
    CARD_ACTION_DIGEST_V1_FIELDS as _CARD_ACTION_DIGEST_V1_FIELDS,
    CARD_ACTION_DIGEST_V2_FIELDS as _CARD_ACTION_DIGEST_V2_FIELDS,
    CARD_IDEA_CONCEPT_FIELDS as _CARD_IDEA_CONCEPT_FIELDS,
    CARD_STEERING_CONTEXT_FIELDS as _CARD_STEERING_CONTEXT_FIELDS,
    CARD_STATEMENT_MAX_CHARS as _CARD_STATEMENT_MAX_CHARS,
    CARD_STATEMENT_MAX_UTF8_BYTES as _CARD_STATEMENT_MAX_UTF8_BYTES,
    Card,
    CardConceptSource as _CardConceptSource,
    CardIdentityProvenance as _CardIdentityProvenance,
    CardSelectionBlocker as _CardSelectionBlocker,
    CardSelectionProvenance as _CardSelectionProvenance,
    DEVELOPER_FOOTPRINT_MARKER as _DEVELOPER_FOOTPRINT_MARKER,
    IDEA_PROPOSAL_DIGEST_V1_FIELDS as _IDEA_PROPOSAL_DIGEST_V1_FIELDS,
    _card_action_digest as __card_action_digest,
    card_action_digest as _card_action_digest_v2,
    card_ownership_receipt as _card_ownership_receipt,
    card_score_fence_state as _card_score_fence_state,
    developer_artifact_footprint as _developer_artifact_footprint,
    durable_idea_payload as _durable_idea_payload,
    effective_card_footprint as _effective_card_footprint,
    hypothesis_concept_cache_keys as _hypothesis_concept_cache_keys,
    hypothesis_id as _hypothesis_id,
    hypothesis_statement_digest,
    idea_proposal_digest as _idea_proposal_digest,
    idea_proposal_ref as _idea_proposal_ref,
    legacy_card_action_digest_v1 as _legacy_card_action_digest_v1,
    legacy_card_ownership_receipt_v1 as _legacy_card_ownership_receipt_v1,
    normalize_researcher_footprint,
    normalize_steering_context as _normalize_steering_context,
    normalized_hypothesis_statement as _normalized_hypothesis_statement,
    surviving_work_item_aliases as _surviving_work_item_aliases,
    transitional_card_action_digest_v1 as _transitional_card_action_digest_v1,
    transitional_card_ownership_receipt_v1 as _transitional_card_ownership_receipt_v1,
    valid_card_action_digest as _valid_card_action_digest,
    valid_researcher_footprint,
)
from looplab.core.concepts import (
    CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON as _CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
    CONCEPT_MATERIALIZATION_REASONS as _CONCEPT_MATERIALIZATION_REASONS,
    ConceptMaterializationReceipt,
    ConceptMaterializationReason as _ConceptMaterializationReason,
    bounded_raw_concept_values,
    concept_materialization_receipt as _concept_materialization_receipt,
    concept_materialization_reason as _concept_materialization_reason,
    normalize_concept_id,
    normalized_concept_materialization_receipt as _normalized_concept_materialization_receipt,
    valid_concept_id,
)
from looplab.core.fitness import is_better as _is_better, is_usable_metric

# Compatibility/public import seam: card identity (the versioned digests, the ownership receipts, the
# footprint/steering vocabularies they bind, and the Card provenance family) lives in core.cards, while
# historical consumers import domain contracts from core.models. Explicit assignments keep that API
# stable without duplicate logic — `looplab.core.models.<name>` and `looplab.core.cards.<name>` are the
# SAME object, so every existing import site and monkeypatch seam keeps resolving.
CARD_ACTION_DIGEST_V1_FIELDS = _CARD_ACTION_DIGEST_V1_FIELDS
CARD_ACTION_DIGEST_V2_FIELDS = _CARD_ACTION_DIGEST_V2_FIELDS
CARD_IDEA_CONCEPT_FIELDS = _CARD_IDEA_CONCEPT_FIELDS
CARD_STEERING_CONTEXT_FIELDS = _CARD_STEERING_CONTEXT_FIELDS
CARD_STATEMENT_MAX_CHARS = _CARD_STATEMENT_MAX_CHARS
CARD_STATEMENT_MAX_UTF8_BYTES = _CARD_STATEMENT_MAX_UTF8_BYTES
CardConceptSource = _CardConceptSource
CardIdentityProvenance = _CardIdentityProvenance
CardSelectionBlocker = _CardSelectionBlocker
CardSelectionProvenance = _CardSelectionProvenance
DEVELOPER_FOOTPRINT_MARKER = _DEVELOPER_FOOTPRINT_MARKER
IDEA_PROPOSAL_DIGEST_V1_FIELDS = _IDEA_PROPOSAL_DIGEST_V1_FIELDS
card_action_digest = _card_action_digest_v2
card_ownership_receipt = _card_ownership_receipt
card_score_fence_state = _card_score_fence_state
developer_artifact_footprint = _developer_artifact_footprint
durable_idea_payload = _durable_idea_payload
effective_card_footprint = _effective_card_footprint
hypothesis_concept_cache_keys = _hypothesis_concept_cache_keys
hypothesis_id = _hypothesis_id
idea_proposal_digest = _idea_proposal_digest
idea_proposal_ref = _idea_proposal_ref
legacy_card_action_digest_v1 = _legacy_card_action_digest_v1
legacy_card_ownership_receipt_v1 = _legacy_card_ownership_receipt_v1
normalize_steering_context = _normalize_steering_context
normalized_hypothesis_statement = _normalized_hypothesis_statement
surviving_work_item_aliases = _surviving_work_item_aliases
transitional_card_action_digest_v1 = _transitional_card_action_digest_v1
transitional_card_ownership_receipt_v1 = _transitional_card_ownership_receipt_v1
valid_card_action_digest = _valid_card_action_digest
# The versioned minter itself is private, but `tests/test_digest_and_number_contracts.py` reaches it
# HERE to pin that the shared preimage cap is still passed, so the seam covers it too.
_card_action_digest = __card_action_digest

# Compatibility/public import seam: receipt ownership lives in core.concepts, while historical consumers
# import domain contracts from core.models. Explicit assignments keep that API stable without duplicate logic.
CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON = _CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON
CONCEPT_MATERIALIZATION_REASONS = _CONCEPT_MATERIALIZATION_REASONS
ConceptMaterializationReason = _ConceptMaterializationReason
concept_materialization_receipt = _concept_materialization_receipt
concept_materialization_reason = _concept_materialization_reason
normalized_concept_materialization_receipt = _normalized_concept_materialization_receipt


def normalize_extra_metrics(value, *, max_items: int = 256) -> dict[str, float]:
    """Normalize the public multi-objective metric map to finite scalar JSON numbers.

    Evaluation stdout and old event logs are untrusted JSON.  Bookkeeping objects/lists occasionally landed
    in ``extra_metrics`` even though every consumer (Pareto UI, MLflow, schemas) treats values as scalars;
    Pydantic then warned on every API serialization.  The append-only raw event retains those values for
    audit, while the folded/public model exposes only its documented numeric contract.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        if len(out) >= max_items or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        try:
            number = float(raw)
        except (TypeError, OverflowError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key)[:200]] = number
    return out


MAX_LESSON_NODE_COUNT = (1 << 31) - 1


def coerce_node_id(d: dict, key: str = "node_id"):
    """Coerce a raw event `node_id` to an int for a fold KEY/membership op, or None if it isn't a usable
    node id. Several sanctioned /control events (`approval_granted`, `annotation`) are appended VERBATIM,
    so a forged `{"node_id":[999]}` (unhashable) / bool / non-numeric id must be rejected BEFORE it
    reaches a dict/set hash — else the fold raises `TypeError: unhashable` and bricks every replay. Rejects
    a bool (subclasses int, so int(True)==1 would spuriously match node 1) and anything non-coercible
    (incl. a non-finite float -> OverflowError). A missing/None id also returns None; each handler decides
    whether that means accept (a bare grant) or drop."""
    # Lives here rather than in `events/replay.py` (its historical home, still reachable as
    # `replay._coerce_node_id`) because the Card ledger extracted to `events/card_ledger.py` bounds
    # the same untrusted ids and `events` modules must not import each other in a cycle to do it —
    # the same reason `is_unevaluated_speculative_discard` sits beside `Node` instead of in `search`.
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


# Stable replay-derived trust labels for ``RunState.node_concept_provenance``.  Keep these strings
# boring and explicit: the novelty admission path compares them exactly and treats every future /
# malformed / missing value as untrusted until that producer is reviewed.
NODE_CONCEPT_PROVENANCE_AUTHORED = "researcher-authored"
NODE_CONCEPT_PROVENANCE_CLASSIFIER = "classifier"
# ``concept-coverage --offline --persist`` restores a useful display taxonomy on old runs, but its
# alias matcher is not an independent semantic classifier.  Keep the exact producer visible while
# excluding it from evidence consumers through ``classifier_verified_node_concepts``.
NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC = "offline-heuristic"
# A future/malformed ``node_concepts.mode`` must not inherit classifier trust merely because replay
# understands the event type.  Preserve memberships for forward-compatible read models while
# collapsing the unknown producer to one explicit, permanently non-evidence label.
NODE_CONCEPT_PROVENANCE_UNTRUSTED = "untrusted-source"
# PART V Phase 2b: an OPERATOR manually re-tagged this node's concepts. Authoritative for the run's
# READ MODELS (UI/tools) and NOT clobbered by the classifier re-tag cadence — but deliberately NOT treated
# as independent classifier EVIDENCE (classifier_verified_node_concepts stays classifier-only), so a human
# curation edit never silently becomes cross-run/novelty evidence without its own review.
NODE_CONCEPT_PROVENANCE_OPERATOR = "operator-edited"

# The provenance tiers whose concept set is an EXACT membership statement, and therefore the ones a
# child may inherit through (doc 25 EV-11). An explicit full-set producer may be low-trust display
# taxonomy and still define inheritance (offline heuristic), but an unknown/future producer or a
# missing provenance is not an exact set — those force the delta unavailable rather than guessing.
#
# Spelled ONCE because `_materialize_concept_deltas` consults it from two passes over the same log:
# the Kahn topological walk and the cycle fallback. If those two disagree about which tiers are
# inheritable, the same event log folds to different concept memberships depending only on whether
# the node graph happened to contain a cycle — a replay-determinism break with no error anywhere.
# It sits beside the tier constants (rather than in `events/replay.py`, its historical home) because
# the Card ledger in `events/card_ledger.py` derives its own display set from it, and the two
# `events` modules must not import each other in a cycle to share one frozenset.
INHERITABLE_CONCEPT_PROVENANCE = frozenset({
    NODE_CONCEPT_PROVENANCE_AUTHORED,
    NODE_CONCEPT_PROVENANCE_CLASSIFIER,
    NODE_CONCEPT_PROVENANCE_OPERATOR,
    NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
})

# A folded concept membership can be deliberately empty (an honest, known-empty set) or empty because
# replay could not materialize an invalid delta dependency graph.  Keep that distinction in a typed,
# reader-defaulted receipt instead of forcing every downstream projection to reverse-engineer the DAG.
def classifier_verified_node_concepts(state: Any, node_id: int) -> list[str]:
    """Return concept memberships backed by the independent classifier.

    ``Idea.concepts`` and classifier output intentionally share the public ``node_concepts`` read-model
    for UI compatibility.  Any consumer that turns those labels into admission or cross-run evidence must
    cross the provenance sidecar through this helper so missing, malformed, and future producers fail closed.
    """
    provenance = getattr(state, "node_concept_provenance", None) or {}
    # only the exact reviewed producer is evidence; proposer-authored labels remain display-only.
    if provenance.get(node_id) != NODE_CONCEPT_PROVENANCE_CLASSIFIER:
        return []
    receipts = getattr(state, "node_concept_materialization_receipts", None) or {}
    if node_id in receipts:
        # A classifier may have produced some valid labels while also overflowing the bound or emitting
        # malformed ids. The retained subset is useful UI data, but it is not a complete evidence set.
        return []
    memberships = getattr(state, "node_concepts", None) or {}
    return list(memberships.get(node_id) or [])


def node_concept_event_provenance(data: Any) -> str:
    """Resolve a durable ``node_concepts`` producer without guessing.

    Historical cadence events predate ``mode`` and were emitted only by the reviewed classifier,
    so an *absent* field retains classifier trust.  Current classifier writers use one of the two
    exact modes below.  The exact offline fallback is display-only, and every explicit unknown,
    malformed, or future value fails closed as untrusted until that producer is reviewed.
    """
    if not isinstance(data, dict):
        return NODE_CONCEPT_PROVENANCE_UNTRUSTED
    if "mode" not in data:
        return NODE_CONCEPT_PROVENANCE_CLASSIFIER
    mode = data.get("mode")
    if mode in ("llm", "agentic"):
        return NODE_CONCEPT_PROVENANCE_CLASSIFIER
    if mode == "offline-heuristic":
        return NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC
    # explicit-but-unknown is not legacy. Treating it like an absent legacy field would
    # let a typo or future producer silently enter graded-novelty and cross-run evidence.
    return NODE_CONCEPT_PROVENANCE_UNTRUSTED


def safe_lesson_node_count(value) -> int | None:
    """Total parser for a durable advisory node-count watermark.

    Current writers emit integers. Lossless numeric legacy scalars remain accepted, while malformed or
    enormous values cannot crash resume or suppress lesson/reflection cadence forever.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 10 or not text.isascii() or not text.isdecimal():
            return None
        result = int(text)
    else:
        return None
    return result if 0 <= result <= MAX_LESSON_NODE_COUNT else None


def latest_lesson_node_count(records, *, key: str = "at_node") -> int:
    """Largest valid watermark in heterogeneous durable rows; invalid rows contribute nothing."""
    latest = 0
    for record in records or ():
        if not isinstance(record, dict):
            continue
        parsed = safe_lesson_node_count(record.get(key))
        if parsed is not None:
            latest = max(latest, parsed)
    return latest


class NodeStatus(str, Enum):
    pending = "pending"      # node_created seen, not yet evaluated (resume re-entry point)
    evaluated = "evaluated"  # has a metric
    failed = "failed"        # ran but produced no usable metric


class Idea(BaseModel):
    """A proposed experiment: which operator, what parameters, why."""
    operator: str
    params: dict[str, float] = Field(default_factory=dict)
    rationale: str = ""
    # RepoTask Phase 2: the Researcher may pick which eval profile to run (e.g. cheap
    # "smoke" during search vs "full" on confirm) — eval depth is part of the action space.
    eval_profile: Optional[str] = None
    # Per-node eval wall-clock budget (seconds) the Researcher may set for THIS experiment — e.g. a
    # neural-net / large-ensemble idea that legitimately needs longer than the run's default `timeout`.
    # Honored by the engine ONLY when the governance matrix grants the researcher the "timeout" setting
    # (Settings.agent_control); otherwise ignored. None => use the run-wide timeout. Flows through the
    # event log on the Idea automatically (no new event), so it's replay-safe.
    eval_timeout: Optional[float] = None
    # Semantic grouping (UI #7): a short, reusable slug the Researcher assigns to cluster related
    # experiments in one search tree (e.g. "loss-fn", "architecture", "regularization"). Optional
    # and neutral for direct metric ranking. It still feeds breadth/strategy context as a legacy
    # fallback when no multi-label `concepts` are available, so it can steer later proposals. Flows through the
    # event log automatically (idea.model_dump → node_created → Idea(**d["idea"]) in replay.fold).
    # DEPRECATED by `concepts` (below) — the multi-label concept graph supersedes the single theme
    # slug; kept only until every theme consumer is migrated off it. No longer authored.
    theme: Optional[str] = None
    # PART IV concepts: the SET of research concepts this experiment touches, as `axis/slug` ids
    # (e.g. "loss/contrastive", "architecture/moe", "regularization/r-drop"). The Researcher AUTHORS
    # these (many-to-many — a node usually touches several; propose a new id when none fits). This is
    # the grouping substrate that replaces the flat `theme` slug. These are PROPOSER claims, not
    # independent classifier evidence. Folded into RunState.node_concepts at node_created so concept
    # read-models see them from the first node; RunState.node_concept_provenance keeps that trust boundary
    # explicit and a later classifier event may consolidate/enrich them.
    # Flows through the event log automatically (idea.model_dump → node_created → Idea(**d["idea"])).
    concepts: list[str] = Field(default_factory=list)
    # PART V (B) run-base + node-DELTA authoring. The discriminator is semantic: `delta` makes the two
    # delta lists authoritative EVEN WHEN BOTH ARE EMPTY (inherit without changing anything); `full`
    # makes `concepts` the exact membership. Reader-side absence preserves old Idea payloads.
    # never infer this choice from list truthiness or serializer field presence — either
    # collapses an explicit zero delta into an absent legacy membership.
    # this is the tolerant durable reader. Absent is distinct from authoritative full+[];
    # modern producers cross the required/closed IdeaEmission boundary below.
    concept_mode: Optional[str] = None
    # In delta mode, instead of re-stating the full `concepts` set, a node may
    # author only what CHANGES vs the run base + its parents — `concepts_added` (new this node) and
    # `concepts_removed` (dropped this node, e.g. "swapped transformer -> diffusion"). The fold post-pass
    # materializes node_concepts = inherited − removed + added (inherited = run base at a root, else the
    # union of parents' effective sets); `concepts` (full set) is ignored for that node.
    # The explicit mode, rather than list truthiness, selects this path.
    concepts_added: list[str] = Field(default_factory=list)
    concepts_removed: list[str] = Field(default_factory=list)
    # Intra-node sweep: instead of a single point in `params`, the Researcher may attach a discrete
    # search GRID here {name: [values...]}. When non-empty, the Developer renders code that runs
    # every grid point in ONE process (shared data load / warm GPU) and reports all results back as
    # node.trials in a single node_evaluated event. `params` may still carry fixed/shared
    # hyperparameters alongside the swept grid. Grids only (not ranges) to keep the model union-free
    # and the enumeration deterministic for replay — a future `space_kind` field can add ranges.
    space: dict[str, list[float]] = Field(default_factory=dict)

    # Hypothesis ledger (P1): a one-line statement of WHAT THIS EXPERIMENT TESTS ("residual features
    # help", "a deeper tree overfits here"). Optional and neutral for direct metric ranking, but the
    # resulting open board is prompt input: it turns the search from "propose the next mutation" into
    # "run experiments that resolve open questions". When set, the fold derives/links a Hypothesis
    # (id = slug of the statement) and tracks it to a verdict from the
    # node's outcome. Flows through the event log on the Idea automatically; None => today's behavior.
    hypothesis: Optional[str] = None
    # Hypothesis-card Kanban re-architecture (docs/23, Layer 1a): the STABLE card id this experiment
    # belongs to. When set, `_derive_cards` links this node's evidence to the card by id (robust to
    # statement paraphrase); when None (legacy logs / not-yet-minted), it falls back to the statement
    # hash exactly like `_derive_hypotheses`. Additive + nullable, so it rides `durable_idea_payload` ->
    # node_created -> Idea(**d["idea"]) for free and old logs fold identically. The engine stamps it from
    # its receipt-bound Card mint; legacy/external writers may still leave it absent.
    card_id: Optional[str] = None
    # Hypothesis-card Kanban (docs/23, Layer 1b): the Researcher-PROPOSED resource footprint for this
    # experiment — {gpus, gpu_mem_mib, ...}. Audit-only in Layer 1 (surfaced on the card as proposed_by=
    # 'researcher'); the Developer FINALIZES it and the bin-packing scheduler CONSUMES it only in Layer 4.
    # Additive + nullable, rides durable_idea_payload -> node_created -> Idea(**d["idea"]) for free (like
    # eval_profile); None => today's behavior. Timeout is NOT here — it stays the single canonical
    # eval_timeout, clamped to a Settings ceiling (docs/23 owner decision 3).
    footprint: Optional[dict] = None

    @field_validator("card_id", mode="before")
    @classmethod
    def _read_bounded_card_id(cls, value):
        # card linkage is advisory. A future/corrupt scalar must not reject node_created and
        # thereby change best-selection; only a bounded, printable string can participate in the join.
        if value is None or not isinstance(value, str):
            return None
        card_id = value.strip()
        if not card_id or len(card_id) > 256 or not card_id.isprintable():
            return None
        return card_id

    @field_validator("footprint", mode="before")
    @classmethod
    def _read_researcher_footprint(cls, value):
        return normalize_researcher_footprint(value)

    @property
    def is_sweep(self) -> bool:
        return bool(self.space)

    @model_serializer(mode="wrap")
    def _omit_absent_concept_mode(self, handler):
        # Pydantic 2.6-compatible nested serialization rule. This covers Node/RunState dumps too;
        # Field(exclude_if=...) is newer than the project's supported floor.
        payload = handler(self)
        if self.concept_mode is None and isinstance(payload, dict):
            payload.pop("concept_mode", None)
        return payload

    @field_validator("concepts", "concepts_added", "concepts_removed", mode="before")
    @classmethod
    def _read_bounded_concept_list(cls, value):
        # Historical/future logs are untrusted input: a malformed list must not drop the whole node, and
        # an enormous list must not make each descendant copy an ever-growing membership. The raw event
        # remains the audit record; the folded reader keeps the canonical lexical top 64.
        bounded, _overflow, _invalid = bounded_raw_concept_values(value)
        return bounded

    @field_validator("concepts", "concepts_added", "concepts_removed", mode="after")
    @classmethod
    def _drop_malformed_concepts(cls, v):
        # Concept ids are a bounded axis/slug taxonomy. Silently drop malformed AUTHORED ids (base64/hash
        # garbage, symbols, emoji — e.g. an observed real-run tag) so a proposer/LLM hallucination never
        # pollutes node_concepts, the /concepts tree, or (via the classifier) cross-run capsules. Gate the
        # full and both delta paths identically; otherwise switching to delta silently bypasses this trust
        # boundary. Runs at
        # fold too — the Idea is rebuilt via Idea(**d["idea"]) — so it deterministically heals old logs;
        # legitimate ids (incl. non-ASCII letters) pass unchanged.
        return [c for c in v if valid_concept_id(c)] if isinstance(v, list) else v

    @field_validator("concept_mode", mode="before")
    @classmethod
    def _read_future_concept_mode(cls, value):
        # Durable readers are total over future/corrupt discriminators. Replay inspects raw presence
        # and stamps an untrusted receipt; retaining a bounded spelling here keeps the node auditable.
        if value is None:
            return None
        if isinstance(value, str):
            return value[:80]
        return f"unsupported:{type(value).__name__}"[:80]

    @model_validator(mode="after")
    def _backfill_rationale(self) -> "Idea":
        # An idea's `rationale` is the human-readable "why" the UI panel shows. Researchers sometimes
        # emit a structural idea (a code change, not a param sweep) with a filled `hypothesis` but an
        # empty `rationale` — leaving the node with no visible description. When that happens, derive
        # the rationale from the hypothesis so every node always carries a "why". Runs replay-safe:
        # fold rebuilds ideas through this validator, so it also heals such nodes in existing runs.
        if not (self.rationale or "").strip() and (self.hypothesis or "").strip():
            self.rationale = self.hypothesis.strip()[:500]
        return self

    @model_validator(mode="after")
    def _clamp_params_to_space(self) -> "Idea":
        # Safety net: clamp any `params` value that falls OUTSIDE its declared `space` bound back into
        # range. A mutation/latent-sampling path occasionally leaked raw out-of-range values into the
        # idea — e.g. lr_stage2=-0.0204, temperature=-0.0119, batch_size=17541 with space
        # lr_stage2=[3e-4, 1e-3] — and the Developer either crash-implemented them or wasted reasoning
        # decoding "why is the learning rate negative" (live nodes 59, 61 both crashed off this). Only
        # values strictly outside [lo, hi] are touched, so valid points pass through untouched; replay
        # rebuilds ideas through this validator, healing such params in existing logs too.
        for k, val in list(self.params.items()):
            rng = self.space.get(k)
            if not (isinstance(rng, (list, tuple)) and len(rng) >= 2):
                continue
            try:
                lo, hi = float(min(rng)), float(max(rng))
                v = float(val)
            except (TypeError, ValueError, OverflowError):
                continue
            if v < lo or v > hi:
                self.params[k] = round(min(hi, max(lo, v)), 6)
        return self

    @field_validator("eval_timeout", mode="before")
    @classmethod
    def _coerce_eval_timeout(cls, v):
        # `eval_timeout` is LLM-proposed and its ONLY consumer treats a non-positive/non-finite value as
        # "unset" (engine/eval_dispatch: `if etv and etv > 0`). COERCE such values to None rather than
        # REJECT them: the fold rebuilds every idea through this validator, so a hard `gt=0`/`allow_inf_nan`
        # constraint would raise ValidationError inside `Idea(**d["idea"])` and silently DROP a node when
        # replaying an old log that carried eval_timeout ∈ {0, negative, inf, nan} — an invariant-5
        # back-compat break (old logs must fold as before). Coercing keeps the "0 => use run default"
        # semantics the consumer already honors, on both the live and replay paths, in one place.
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return None
        return f if math.isfinite(f) and f > 0 else None


class IdeaEmission(Idea):
    """Strict modern producer schema; durable replay intentionally continues to use ``Idea``."""

    model_config = ConfigDict(extra="forbid")

    concepts: list[str] = Field(default_factory=list, max_length=64)
    concept_mode: Literal["full", "delta"]
    concepts_added: list[str] = Field(default_factory=list, max_length=64)
    concepts_removed: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _strict_raw_concept_envelope(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Idea emission must be an object")
        raw_card_id = value.get("card_id")
        if raw_card_id is not None:
            if (not isinstance(raw_card_id, str) or raw_card_id != raw_card_id.strip()
                    or not raw_card_id or len(raw_card_id) > 256
                    or not raw_card_id.isprintable()):
                raise ValueError("card_id must be a bounded printable string")
        raw_footprint = value.get("footprint")
        if raw_footprint is not None and not valid_researcher_footprint(raw_footprint):
            raise ValueError("footprint must contain only bounded integer gpus/gpu_mem_mib")
        for field in ("concepts", "concepts_added", "concepts_removed"):
            raw = value.get(field, [])
            if not isinstance(raw, list):
                raise ValueError(f"{field} must be a JSON list")
            if len(raw) > 64:
                raise ValueError(f"{field} may contain at most 64 ids")
            if any(not isinstance(item, str) or not valid_concept_id(item) for item in raw):
                raise ValueError(f"every {field} item must be a bounded axis/slug")
        return value

    @field_validator("concepts", "concepts_added", "concepts_removed", mode="before")
    @classmethod
    def _strict_concept_list(cls, value):
        # the tolerant reader heals old logs, but a modern writer must retry malformed ids.
        # Otherwise base Idea's drop-validator could turn full+[bad] into authoritative known-empty or
        # delta+[bad] into a semantically different zero delta.
        if not isinstance(value, list):
            raise ValueError("concept fields must be JSON lists")
        if len(value) > 64:
            raise ValueError("concept fields may contain at most 64 ids")
        if any(not isinstance(item, str) or not valid_concept_id(item) for item in value):
            raise ValueError("every concept id must be a bounded axis/slug")
        canonical = [normalize_concept_id(item) for item in value]
        if len(set(canonical)) != len(canonical):
            raise ValueError("concept fields cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _consistent_concept_envelope(self) -> "IdeaEmission":
        if self.concept_mode == "full" and (self.concepts_added or self.concepts_removed):
            raise ValueError("full concept_mode cannot carry concepts_added/concepts_removed")
        if self.concept_mode == "delta" and self.concepts:
            raise ValueError("delta concept_mode cannot carry a full concepts list")
        added = {normalize_concept_id(item) for item in self.concepts_added}
        removed = {normalize_concept_id(item) for item in self.concepts_removed}
        if added & removed:
            raise ValueError("one concept cannot be both added and removed")
        return self

    def to_idea(self) -> Idea:
        """Cross the strict writer boundary into the forward-compatible durable model."""
        return Idea.model_validate(self.model_dump(mode="json"))


# The optimization direction a task's metric is scored under. There is no safe default: a task that
# means "maximize accuracy" but is read as "minimize" makes the search chase its WORST candidate and
# report it as best, and nothing downstream can detect that — every number involved is real.
#
# The validator existed on 2 of the 9 task models (doc 25 RA-06), so the other seven accepted
# `direction="mxa"` silently and pydantic's `str` field happily stored it. Every model now attaches
# this one; `mlebench_real` additionally allows "auto", which it resolves from the grader before the
# value reaches any comparison.
DIRECTIONS = ("min", "max")


def validate_direction(value, *, extra: tuple[str, ...] = ()):
    """Reject anything that is not an exact known direction, naming what was received."""
    allowed = (*DIRECTIONS, *extra)
    if value not in allowed:
        raise ValueError(
            f"direction must be one of {', '.join(repr(name) for name in allowed)}, got {value!r}")
    return value


# The Developer-crash sentinel. A Developer that cannot finish returns its error IN BAND as the
# node's code — a graceful "this build produced nothing usable", distinct from an exception, which
# means the engine itself broke. Six consumers (orchestrator ×3, node_build, speculation ×2) key a
# terminal/no-terminal decision on it, and getting that wrong turns a crash into a FALSE SUCCESS: a
# node recorded as evaluated whose code is an error message.
#
# It was a bare literal at the producer and at every consumer (doc 25 ES-11), so a backend that
# worded its error differently — or a format tweak here — would have silently flipped all six
# without a single failing test. Producer and consumers now share this constant, and
# `tests/test_developer_error_sentinel.py` pins that no consumer re-spells it.
DEVELOPER_ERROR_PREFIX = "(developer error:"

# Every reason an eval can produce no usable metric — the closed vocabulary
# `engine/triage.py::_failure_reason` classifies into and `Settings.inline_repair_reasons` selects
# from. It lives HERE, in core, because `core/config.py` needs it for that default and core may not
# import from `engine`; `triage.py` re-exports it so the classifier and its vocabulary still read as
# one thing. A registry rather than literals copied into the classifier, the setting, the engine
# options and the settings docs — the failure mode of the copies is silent, and it has already
# happened once: `no_metric` was in the classifier and absent from the default set, so that whole
# class of failure was never repaired and nobody had decided it should not be.
# `needs_failed` is the INPUT contract's twin of `expect_failed` and is separate for the same kind of
# reason: the stage never RAN, so nothing about its own code is implicated — the repair is either an
# earlier stage that wrote its output elsewhere or a declaration that names the wrong path, and
# telling the Developer "your stage crashed" would send it to read code that never executed.
# `expect_failed` / `check_failed` are deliberately NOT folded into `no_metric`: the command did
# not "run cleanly and print nothing", it ran and then failed a contract its own manifest
# declared. Measured cost of the conflation — rubertlite-dr-unified-v5 node 0 died as
# `no_metric` having printed its metric, and the operator was told the command printed none.
# `diverged` and `stalled` are the two WATCHDOG verdicts, and they are separate members for the same
# reason: both are tree-kills the ENGINE issued, so both exit -9 with no traceback — byte-identical to
# the kernel OOM signature `_failure_reason` recognises. Measured cost of that conflation on
# rubertlite-dr-unified-v6 node 5: the DIVERGE watchdog stopped a training whose loss went 1.2e+25
# then NaN, the classifier called it `oom`, and the Developer spent three repair rounds halving the
# batch size (8192 -> 2048 -> 512 -> 256) at ~3 GPU-minutes each while the actual instability went
# untouched. "Reduce memory" and "stabilise the numerics" are opposite directives.
FAILURE_REASONS: tuple[str, ...] = ("crash", "timeout", "oom", "setup", "no_metric", "drift",
                                    "expect_failed", "check_failed", "diverged", "stalled",
                                    "needs_failed")


def is_developer_error(code) -> bool:
    """True when `code` is the in-band Developer-crash sentinel rather than real solution code."""
    return isinstance(code, str) and code.startswith(DEVELOPER_ERROR_PREFIX)


# THE DEVELOPER'S OWN "I DO NOT KNOW HOW TO FIX THIS" (F8). Its sibling above says the developer's
# SESSION failed; this one says the session worked fine and the model has no fix left to try. They
# are different facts and they must not share a spelling: `(developer error: …)` routes to the
# provider circuit breaker and pauses the RUN, which is exactly the wrong answer for a healthy model
# that has simply run out of ideas about one node.
#
# It exists because nothing asked. The repair loop's only stop signals were the triage judge and a
# COUNT, so a Developer that knew it was beaten had exactly one way to say so — return another fix it
# did not believe in — and every such non-fix looked to the counter like an ordinary attempt. That is
# half of the 2,345-repair runaway: 369 distinct error signatures, none of which any participant was
# allowed to call hopeless.
#
# WHY AN IN-BAND SENTINEL IS SAFE HERE, when `engine/metric_salvage.py`'s rule is that the agent
# writes the very text an extractor reads. Because this signal is MONOTONE IN THE SAFE DIRECTION: its
# only effect is to END the node. It cannot move a metric, a champion, selectability or a violation —
# the node terminalizes carrying the eval's own authenticated `reason`, exactly as an `abandon` does.
# A model that forges it wastes its own node and gains nothing; a model that forges the opposite
# (never declaring stuck) is the behaviour we already have. Contrast `docs/36`'s table: this decides
# what to do NEXT, never what the result WAS.
DEVELOPER_STUCK_PREFIX = "(developer stuck:"


def is_developer_stuck(code) -> bool:
    """True when `code` is the Developer's own out-of-ideas declaration rather than a repair.

    Deliberately a PREFIX test on the stripped text and nothing cleverer: a model that wants to
    give up says so on the first line, and any "does this text sound hopeless?" heuristic over a
    repair's prose is the same category error as the deleted error-signature normalizer — a rule
    over TEXT QUALITY standing in for a judgement."""
    return isinstance(code, str) and code.strip().startswith(DEVELOPER_STUCK_PREFIX)


def developer_stuck_reason(code) -> str:
    """The Developer's own words about why it is stuck, or "" when `code` is not that declaration.

    Total and lossy on purpose — the reason is prose for the terminal event and for the operator,
    never a value anything branches on."""
    if not is_developer_stuck(code):
        return ""
    body = code.strip()[len(DEVELOPER_STUCK_PREFIX):].strip()
    return body[:-1].strip() if body.endswith(")") else body


class Trial(BaseModel):
    """One configuration evaluated inside an intra-node sweep. Audit/UI data — the node's scalar
    `metric` is set (by the engine) from the best feasible trial, so fold/best-selection are
    untouched."""
    params: dict[str, float] = Field(default_factory=dict)
    metric: Optional[float] = None
    seconds: Optional[float] = None
    extra_metrics: dict[str, float] = Field(default_factory=dict)
    error: str = ""

    @field_validator("extra_metrics", mode="before")
    @classmethod
    def _normalize_extra_metrics(cls, value):
        return normalize_extra_metrics(value)


class Node(BaseModel):
    """A node in the search DAG. `parent_ids` is a list to allow merges (P2)."""
    id: int
    parent_ids: list[int] = Field(default_factory=list)
    # Fold-internal lineage receipt: the exact parent lifecycles this node was built from. Node ids
    # survive reset, so looking up a parent's CURRENT attempt later can silently rewrite history.
    # Public state keeps its compact parent_ids projection; the W3C-PROV export consumes this sidecar.
    parent_generations: dict[str, int] = Field(default_factory=dict, exclude=True)
    operator: str
    idea: Idea
    code: str = ""
    # Multi-file solutions (ADR-7 patch-gated agent): extra in-surface files the agent
    # created/edited besides solution.py. Materialized into the eval workdir. `code`
    # remains the solution.py entrypoint the sandbox runs.
    files: dict[str, str] = Field(default_factory=dict)
    # In-surface files the agent DELETED (patch-gate accepted the deletion). Applied to the eval
    # workdir after the pristine repo is seeded, so an accepted deletion actually takes effect.
    deleted: list[str] = Field(default_factory=list)
    # Logically deleted via a `node_tombstoned` event (append-only delete, §6.3). The node and its
    # events STAY in the log — so parent links still resolve, the delete is reversible/auditable, and
    # node-id allocation never reuses the id — but a tombstoned node is invisible to selection: the
    # evaluated/feasible/breedable/pending helpers skip it, so it can never be chosen best, bred from,
    # or re-picked for eval. Irreversible physical purge is a separate explicit compaction, never an
    # ordinary domain command. Additive + reader-defaulted: absent on old logs -> False -> unchanged fold.
    tombstoned: bool = False
    metric: Optional[float] = None
    status: NodeStatus = NodeStatus.pending
    # Fold-internal causal anchor for projections that must identify the FIRST accepted terminal of
    # this lifecycle. Excluded from every public model dump: the durable source remains the event log.
    terminal_event_seq: Optional[int] = Field(default=None, exclude=True)
    error: str = ""
    # Failure taxonomy (set by node_failed): setup | timeout | oom | crash | no_metric | drift.
    # Audit/observability only — lets a UI/operator see WHY runs fail across a search.
    error_reason: str = ""
    # Crash-triage verdict (set by node_failed when the LLM triage ran): the agent's one-line
    # judgment of WHY the failure happened / whether the IDEA is at fault — the most expensive
    # reasoning in the failure path. Folded onto the node so the failure-reflection hint and the
    # digest can feed it to the NEXT proposal instead of dropping it (signal-delivery, §1).
    triage_rationale: str = ""
    stdout_tail: str = ""
    # ASHA past-experiment curve (#7): a bounded per-RUNG `[[rung, metric], ...]` (canonical geometric
    # rungs — powers of two — via asha_monitor._resource_rung) mined from the eval's CAPTURED stdout (the
    # ~64 KB run tail — far larger than the 500-char `stdout_tail`, though for a very verbose or
    # multi-stage job not the literal full stream) at node_evaluated, set only when the task declares a
    # stdout_json `resource_key`. The 500-char `stdout_tail` retains only the FINAL epochs, so a live node
    # stopped earlier finds no comparable peers there; this durable curve lets the ASHA watchdog compare a
    # fresh sample against past experiments at the SAME rung across the WHOLE run (the shared rung
    # schedule is what makes a mid-run comparison land). Additive/reader-defaulted (None on old logs).
    # EXCLUDED from the model dump (#7 review): engine-internal ASHA evidence the watchdog reads off the
    # in-process fold — no UI/API consumer reads it, so serializing up to 32 points per node into every
    # lightweight /state and SSE frame was pure O(nodes × curve) transfer. `exclude=True` keeps it off
    # public dumps while the fold still populates the attribute (like `terminal_event_seq`).
    resource_curve: Optional[list] = Field(default=None, exclude=True)
    # Multi-seed confirmation (I12): set by a node_confirmed event. When present,
    # best-selection ranks by confirmed_mean (the robust metric) instead of `metric`.
    confirmed_mean: Optional[float] = None
    confirmed_std: Optional[float] = None
    confirmed_seeds: Optional[int] = None   # how many seeds actually succeeded (I12)
    # D1 holdout-gated promotion (B6): metric of this node on the FINAL holdout partition the
    # search never saw (set by a `holdout_evaluated` event at finish, val-top-k only). When
    # `holdout_select` was recorded on the run, best-selection ranks holdout-carrying nodes by
    # THIS metric — the anti-validation-overfitting gate (AIRA val-test gap 15-16.6%).
    holdout_metric: Optional[float] = None
    # Direction-aware val-vs-robust gap, DERIVED by the fold: how much better the search metric
    # looked than the unseen-signal metric (holdout, else confirmed mean). Positive = the node
    # overperformed on the signal the search optimized — the overfitting indicator the Trust
    # panel surfaces. Audit-only.
    generalization_gap: Optional[float] = None
    # R1-c: a calibrated §12-verifier soundness score in [0,1] for THIS node's realized result. New writers
    # publish the complete tie atomically in `verifier_group_scored`; legacy `node_verified` remains readable.
    # Used ONLY as a tie-break among metric-EQUAL/CI-tied feasible nodes (SearchFitness)
    # — it can never override a strictly-better robust_metric (§21.7 advisory-never-overrides). None
    # otherwise; additive/reader-defaulted so old logs fold byte-identically.
    verifier_score: Optional[float] = None
    eval_seconds: Optional[float] = None     # wall-clock of this node's eval (cost accounting #2)
    # Multi-objective (#5): extra reported metrics + unmet hard constraints. `feasible` is
    # False when any constraint was violated — such a node keeps its metric (for the audit
    # trail) but is excluded from best-selection.
    extra_metrics: dict[str, float] = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)
    feasible: bool = True
    # WHERE THIS NODE'S METRIC CAME FROM, when it was not simply measured. `None` for every ordinary
    # node — a measured metric needs no provenance, and defaulting it that way keeps every old log
    # reading correctly (invariant 5: additive, reader-side default). Set by metric SALVAGE
    # (`engine/metric_salvage.py`), which recovers a metric the eval already produced from a node
    # that failed for some other reason. Folded so `looplab replay` and every read-model can see it:
    # the enforcement rides on `violations` (a salvaged node carries `metric_salvaged` under the
    # default policy and is therefore not `feasible`), but the enforcement is not the EXPLANATION,
    # and a reader asking "why is this node infeasible with a metric" must not have to guess.
    metric_provenance: Optional[dict] = None

    @field_validator("extra_metrics", mode="before")
    @classmethod
    def _normalize_extra_metrics(cls, value):
        return normalize_extra_metrics(value)
    # Transient re-run marker (node_reset): "propose" | "implement" set it so the engine RE-RUNS this
    # existing node in place from that stage; cleared once the re-run's node_created lands. ("eval" resets
    # just clear the terminal — the node becomes pending-with-code and the normal eval loop re-scores it,
    # no marker needed.) Not persisted meaningfully — always None on a settled node.
    rerun_from: Optional[str] = None
    # Multi-stage eval pipeline (Phase 1): per-stage outcomes [{name, status, exit_code, seconds}] in run
    # order (from stage_finished events); `failed_stage` names the stage that broke a failed node. Both
    # empty/None on the classic single-command eval.
    stages: list = Field(default_factory=list)
    failed_stage: Optional[str] = None
    # Phase 2 stage-scoped re-run: the pipeline stage a reset asked to RESTART from (skip earlier stages,
    # reuse their artifacts). Transient — set by node_reset, cleared on the next terminal.
    rerun_stage: Optional[str] = None
    # Immutable lifecycle generation (arch-review §3 P0-1): bumped by every `node_reset`. Every effect
    # derived from work on the node (repair/stage/terminal/confirm/holdout/trust) is stamped with this
    # value and rejected after a newer reset, so an abandoned worker can never adopt or mutate the next
    # lifecycle. The field keeps its original `attempt` name for projection/backward compatibility;
    # new event payloads call the same value `generation` to avoid colliding with node_repaired's
    # pre-existing inline-repair attempt counter. Absent in old logs -> 0.
    attempt: int = 0
    # External-agent audit (ADR-7): set by an `agent_validated` event when the code was
    # produced by a validated CLI-agent Developer. {"ok": bool, "checks": [...]}.
    agent_report: Optional[dict] = None
    # Intra-node sweep results: when the node's idea carried a `space`, the Developer's code ran
    # many configurations in one process and reported them all here. `metric` above is the best
    # feasible trial's metric (computed by the engine), so this list is audit/UI only and never
    # affects search/selection. Empty for ordinary single-config nodes (backward compat).
    trials: list[Trial] = Field(default_factory=list)
    # Cross-run provenance: set when this node was SEEDED from an experiment in a sibling run (via an
    # `import` inject). {"run_id","node_id","metric"} of the source. None for ordinary nodes. Audit/UI
    # only — eval/confirmation/best-selection treat it exactly like any other injected node.
    origin: Optional[dict] = None
    # Deep-research provenance: set when this node was proposed right after a deep-research memo (its
    # directions were the active steering). {"at_node","trigger"} of the memo. None otherwise. Audit/UI
    # only (a 💡 chip) — shows where research landed in the tree; never affects search/selection.
    research_origin: Optional[dict] = None
    # Fold-internal receipt that the Developer finalized this lifecycle's quantitative footprint.
    # Excluded from model dumps so Layer 4 does not perturb snapshots/public DTOs; the append-only
    # node_created/node_repaired event remains the durable authority.
    footprint_finalized: bool = Field(default=False, exclude=True)
    # Layer 5 recovery identity. These fields are deliberately fold-internal: a speculative node is
    # still an ordinary search node at every public boundary, while resume needs an exact durable
    # marker to distinguish a committed producer result from an unrelated build of the same Card.
    speculative: bool = Field(default=False, exclude=True)
    card_build_generation: Optional[int] = Field(default=None, ge=0, exclude=True)
    # Durable "this lifecycle was terminalized BEFORE any evaluation was dispatched" receipt, stamped
    # by the writer on the node's single `node_failed` terminal (additive data field, reader-defaulted
    # -> old logs fold to False and every budget number is byte-identical). It is what lets the L3/L5
    # node-budget REFUND (`is_unevaluated_speculative_discard`, at the foot of this module) be proven from
    # the event log rather than inferred from the absence of a workdir on disk, which replay cannot see.
    # Fold-internal like its two speculative siblings: no public boundary distinguishes a discarded
    # build from any other failed node. It rides the terminal itself, so "first terminal wins" already
    # makes it order-tolerant — no second event has to be correlated with this one.
    never_evaluated: bool = Field(default=False, exclude=True)
    # The two halves of the DURABLE eval-start boundary (events/types.py::EV_NODE_EVAL_STARTED).
    # `eval_start_boundary` is stamped on `node_created` and says the writer of THIS node promises to
    # append a `node_eval_started` row before any sandbox work — so for such a node the ABSENCE of one
    # is evidence, not an assumption. `eval_started` is that row, folded. Together they are what makes
    # "this build never ran" survive a crash: the log used to charge evaluation cost only at the
    # terminal, and `stage_finished` rows used to be appended inside the terminal's own write-lock block
    # too (they moved into the attempt loop on 2026-08-07 — see `engine/evaluate.py` — which narrows the
    # gap but does not close it: a single-command eval and a kill inside the FIRST stage still leave
    # nothing), so a process killed mid-training left a node byte-identical to one that was never
    # dispatched. Both are
    # fold-internal (`exclude=True`) and reader-defaulted, so an old log folds byte-identically — and,
    # carrying no boundary promise, is refused a refund rather than granted one on no evidence.
    eval_start_boundary: bool = Field(default=False, exclude=True)
    eval_started: bool = Field(default=False, exclude=True)

    @property
    def robust_metric(self) -> Optional[float]:
        """The metric used for ranking/display: the multi-seed confirmed mean when present, else the
        raw metric. THE single spelling of "robust metric" — previously copy-pasted at a dozen call
        sites (replay/_select_best, digest, holdout, lessons, exporters, UI, cli, bench), where the
        copies could drift. Holdout precedence deliberately stays OUT of this property: holdout-gated
        selection layers `holdout_metric` on top explicitly (see replay._select_best). A plain
        @property (not a pydantic field/computed_field): excluded from model_dump, so event/snapshot
        serialization is byte-identical."""
        return self.confirmed_mean if self.confirmed_mean is not None else self.metric


def run_setup_key(command) -> str:
    """Stable identity for a run-level `run_setup` command, so a resume can tell "this exact setup
    already completed" from "not yet run" (arch-review §5 P2). A short hash of the canonical argv —
    single-sourced here (core) so the fold (`run_setup_finished` handler) and the engine's skip-check
    compute it identically without a layering violation (events/engine both import core).

    md5, deliberately, and it stays (doc 25 CO-08): the key is compared against one already written
    into a durable `run_setup_finished` event, so changing the hash makes every in-flight run re-run a
    setup it already completed. It is a same-process equality key over a local argv, not an
    authenticated digest — there is no attacker who both controls the argv and benefits from a
    collision that would make their own setup step be SKIPPED."""
    import hashlib
    canon = "\x00".join(str(a) for a in (command or []))
    return hashlib.md5(canon.encode("utf-8")).hexdigest()[:12]


class ResearchMemo(BaseModel):
    """Output of the Deep-Research stage (Phase 2): the model reads a bounded, coverage-aware
    stratified run summary plus configured grounding tools and writes a strategic memo that steers
    the next batch. It is recorded as a
    `research_completed` event folded into `RunState.research`, NEVER as a search-DAG node and never
    directly re-ranks the current champion. Its directions feed later proposal hints, while aligned
    supported claims may feed cross-run evidence at finalization. The UI renders it as a node and
    surfaces `summary`/`findings`/`recommended_directions`; `reasoning` is debug-only."""
    summary: str = ""                                   # one-paragraph conclusion (the takeaway)
    reasoning: str = ""                                 # the "think hard" narrative (debug-only)
    findings: list[str] = Field(default_factory=list)   # concrete observations across results/web
    # D8 evidence ledger: findings as CLAIMS with per-claim provenance — {statement,
    # node_ids: [int], urls: [str]}. Kosmos's failure data says cross-evidence SYNTHESIS is the
    # weakest link (57.9% accurate vs ~85% for analysis), so every synthesis claim must be
    # traceable to the experiments/sources it rests on; the Verifier (trust/memo_verify.py) then
    # checks each claim against its cited evidence and flags the unsupported ones.
    claims: list[dict] = Field(default_factory=list)
    # Sanitizer cardinality receipt for the pre-cap claims list. Excluded from generic model dumps so old
    # state/golden projections stay byte-compatible; the research event writer forwards it explicitly.
    claims_receipt: Optional[dict] = Field(default=None, exclude=True)
    sources: list[dict] = Field(default_factory=list)   # {title, url} consulted (web/arXiv)
    recommended_directions: list[str] = Field(default_factory=list)  # what to try next (steer hints)
    # Optional concrete proposals the engine may materialize as injected nodes (empty for v1; the
    # directions above already feed the Researcher as standing context).
    proposed_ideas: list[Idea] = Field(default_factory=list)
    at_node: Optional[int] = None                       # node count when the stage ran (UI anchor)
    trigger: str = ""                                   # "manual" | "cadence" | "strategist"


class Project(BaseModel):
    """A ClearML-style organizational folder for runs. Projects nest via `parent_id` (None = a
    top-level project). Pure UI metadata stored in `<run-root>/projects.json` — runs never move
    on disk and the engine/event log are untouched (see `projects.ProjectStore`)."""
    id: str
    name: str
    parent_id: Optional[str] = None


class Event(BaseModel):
    """Append-only event envelope = the source of truth ([ADR-1])."""
    v: int = 1            # envelope schema version (ADR-1): lets a future reader migrate old logs
    seq: int = -1
    ts: float = 0.0
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    # Trace correlation (observability): the (trace_id, span_id) active when this event was
    # emitted, so the UI can join the research tree (events) to its execution detail (spans).
    # Diagnostics only — never read by `replay.fold`.
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


class CommentState(BaseModel):
    """Current projection of one event-sourced operator comment.

    The append-only events remain the audit history.  This model intentionally stores only the
    current revision so folding a frequently edited comment does not duplicate every historical text
    inside ``RunState``.  ``RunState.comments`` is excluded from its ordinary JSON dump because the
    live state/SSE surface is intentionally tokenless; authenticated comment routes serialize an
    explicit allow-list instead.
    """

    comment_id: str
    node_id: int
    node_generation: Optional[int] = None
    text: str
    actor_kind: str
    version: int = 1
    resolved: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    created_seq: int = -1
    updated_seq: int = -1
    legacy: bool = False
    editable: bool = True


class RunState(BaseModel):
    """Replay-derived snapshot of the event log ([ADR-12]). ``replay.fold`` is its authoritative
    producer; normal consumers treat the result as immutable. One evaluation recovery path may adjust
    a private, attempt-local snapshot when an old work directory cannot be reused, so folded instances
    must never be cached or shared as mutable process-global state.

    Field regions (docs/15 §P5.3 — banners, deliberately NOT nested sub-models: readers spell
    `st.<field>` at dozens of sites and the flat shape is additive-safe):
      1. core run state (below)            — selection-relevant: nodes/best/gates/budget;
      2. live operator control             — the `<x>_requests`/`<x>s_done` counter pairs (see
         engine invariant #3: every side effect gates on a domain event);
      3. advisory/control receipts         — folded for replay/UI/exports; none directly computes the
                                             current best, but some gate resume/cadence or feed later
                                             prompts, policies and trust enforcement;
      4. read helpers                      — derived views, no mutation."""
    # --- core run state (selection-relevant) ---
    run_id: str = ""
    # Globally unique incarnation identity. ``run_id`` is a display/root-local label and may be reused
    # by independent run roots; cross-run stores key provenance and self-exclusion on this value.
    # Empty on legacy logs, where readers retain the historical run_id fallback.
    run_uid: str = ""
    task_id: str = ""
    goal: str = ""
    direction: str = "min"  # "min" | "max"
    config_hash: str = ""
    # Setup completion, folded from `setup_finished` (arch-review §3 P0-3). run_started is appended in
    # the MIDDLE of setup (before AGENTS.md/provenance/host-grading/profiling and the leakage
    # hard-stop), so gating the setup phase on run_id let a crash right after run_started PERMANENTLY
    # skip the rest of preflight on resume — including leakage enforcement. Gating on setup_done
    # instead makes setup re-run until it actually completes. Absent in old logs -> False; but old logs
    # that already reached the first node also have run_id set, so `_setup_phase` treats a run with any
    # node/finished as already-set-up (see the guard there) — legacy runs never re-run setup.
    setup_done: bool = False
    # P0-3 content-addressed setup: a digest of the MATERIAL setup completed against (config hash +
    # workspace fingerprint + data provenance), folded from `setup_finished`. Binds `setup_done` to the
    # exact inputs so resume can tell "setup done for THIS material" from "done for material that has
    # since changed" — the boolean alone trusted a stale preflight (leakage!). Empty on old logs.
    setup_manifest: str = ""
    # RUN-LEVEL run_setup (dep install) completion, folded from a successful `run_setup_finished`
    # keyed by the command (arch-review §5 P2). Distinct from `setup_done` above: this is the eval's
    # one-time `run_setup` command, not the task/data preflight. The engine's in-memory `_run_setup_done`
    # flag only makes it once-per-PROCESS, so a resume (fresh Engine) re-installs deps every time and a
    # crash mid-setup can't be told from a completed one. Folding the successful command here makes it
    # crash-safe exactly-once across resume. Absent in old logs -> empty set -> setup runs as before.
    run_setup_done: set[str] = Field(default_factory=set)
    # Commands whose `run_setup_started` was folded with no `run_setup_finished` after it — a prior
    # process died mid-install. Their side effects are neither known-applied nor known-absent, and an
    # arbitrary operator command cannot be made transactional from here, so the honest contract is
    # "exactly-once on success, at-least-once across a kill" and the repeat is STAMPED
    # (`after_interrupted_attempt`) instead of masquerading as a first attempt. Hidden from the public
    # RunState dump: this is recovery bookkeeping, and old logs keep their exact serialized shape.
    run_setup_open: set[str] = Field(default_factory=set, exclude=True)
    # T2 trust enforcement (folded from run_started; "audit" for old logs). "gate"/"block" make
    # best-selection exclude nodes flagged for a reward-hack / data-leakage signal (not critic).
    trust_gate: str = "audit"
    # Layer 3 queue owner pinned by run_started. False on old logs preserves the policy/pilot path;
    # replay never infers this selection-affecting treatment from a mutable config snapshot.
    card_driven_selection: bool = Field(False, exclude=True)
    # Layer 5 producer/evaluator overlap. THREE FIELDS, because the run records TWO DIFFERENT FACTS
    # about its depth and one of them is not a run-start pin (they were one field until 2026-08-06,
    # and the conflation made a run that ratcheted itself down unresumable through two doors — see
    # `events/replay.py::_on_speculation_depth_settled` and `core/config.py::RUN_START_PINNED_FIELDS`):
    #
    #   * `speculation_depth_pinned` — what `run_started` recorded, i.e. the LAUNCH treatment
    #     invariant #6 makes immutable. This is the only one of the three that belongs to
    #     `RUN_START_PINNED_FIELDS`, and the only one a re-entry may compare a SPELLED depth against.
    #   * `speculation_depth_settled` — the floor every `speculation_depth_settled` row has ratcheted
    #     the run down to, or None when the run never settled itself. A measurement THIS RUN made
    #     about its own evaluations, not something the operator's launch command owns.
    #   * `speculation_depth` — the EFFECTIVE treatment: the pin narrowed by that floor, which is what
    #     re-entry adopts and what every reader that asks "is this run prefetching" wants. It stays
    #     the plain, publicly-named field precisely because it is the one almost everything reads.
    #
    # Deriving the effective value from two order-INSENSITIVE facts is also what makes the fold
    # order-tolerant against `run_started` itself (invariant #5): a minimum taken directly on this
    # field could not be, because `run_started` ASSIGNS over a default of 0, so a settle row spliced
    # ahead of it folded to the pin instead of the floor.
    # All three hidden from the public RunState dump so legacy/default-zero logs remain byte-identical
    # at the API/golden boundary.
    speculation_depth: int = Field(default=0, ge=0, le=64, exclude=True)
    speculation_depth_pinned: int = Field(default=0, ge=0, le=64, exclude=True)
    speculation_depth_settled: Optional[int] = Field(default=None, ge=0, le=64, exclude=True)
    # Was the pinned depth RESOLVED from the AUTO sentinel, or SPELLED by the operator? Only an AUTO
    # run may ratchet itself down, and until 2026-08-07 that question was answered by a PROCESS
    # attribute (`Engine._speculation_depth_auto`) while the log recorded only the resolved integer.
    # So `looplab run <existing dir>` under the shipped `-1` AUTO default, on a run whose launch
    # SPELLED `speculation_depth=1`, took the AUTO branch and landed a durable, irreversible settle
    # to 0 on someone else's spelled treatment. An absent field folds to False — an old log cannot
    # say, and an irreversible action that costs the run its prefetch must not be inferred from the
    # resuming process's default.
    speculation_depth_auto: bool = Field(default=False, exclude=True)
    # Exact local quality-gate receipt used to admit a positive depth.  Replay keeps the evidence
    # identity separate from the mutable config path; an old positive-depth row without this marker
    # remains readable but is not resumable through the guarded runtime path.
    speculation_gate_receipt_digest: str = Field(default="", exclude=True)
    # Complete source-owned Settings/runtime envelope (including max_nodes, excluding only the
    # treatment depth, receipt placement and output path).  This prevents a valid quality receipt
    # from being replayed under a cheaper/different runtime profile.
    speculation_runtime_scope_sha256: str = Field(default="", exclude=True)
    # Exact shipped runtime that produced a calibration/positive-depth trajectory.  The quality gate
    # rejects evidence without this run-start pin, preventing old run directories from being relabelled
    # with the digest of later code.
    speculation_implementation_digest: str = Field(default="", exclude=True)
    # Restricted bootstrap evidence is a separate authority envelope, never a fake quality receipt.
    # All fields are run-start pins and hidden from the legacy/public projection.
    speculation_calibration_profile_digest: str = Field(default="", exclude=True)
    speculation_calibration_gpu_inventory: list[dict] = Field(default_factory=list, exclude=True)
    speculation_calibration_seed: Optional[int] = Field(default=None, exclude=True)
    # The v1 scorer/evidence envelope is intentionally Greedy-only.  A later broader policy rollout
    # needs its own source-owned scorer matrix and a new receipt scope.
    speculation_policy_scope: str = Field(default="", exclude=True)
    # The SETTLED concurrency widths this run actually started with, pinned by run_started. Both
    # Settings fields ship `0` = AUTO, a SENTINEL resolved off the LIVE BOX (`_detect_gpu_ids`) — so
    # a snapshot storing `0` records the operator's *intent*, never the treatment the log was written
    # under. Resuming a 1-GPU run on a 2-GPU host would then silently double eval concurrency and flip
    # the build spine from serial to the concurrent-append seam MID-LOG (engine invariant #1's
    # byte-order seam), with nothing in the log to say so. Pinning the RESOLVED integer is the same
    # treatment `speculation_depth` already gets, for the same reason (invariant #6).
    # `0` here means NOT RECORDED (old logs) -> the engine neither adopts nor refuses -> byte-identical
    # legacy behaviour. Excluded from the public dump so those old logs keep their exact shape.
    eval_parallel: int = Field(default=0, ge=0, le=1024, exclude=True)
    llm_parallel: int = Field(default=0, ge=0, le=64, exclude=True)
    # D1 holdout-gated promotion (folded from run_started; False for old logs -> byte-identical
    # legacy selection). When True, best-selection prefers the holdout metric among the nodes
    # that carry one (the val-top-k re-scored on the unseen partition at finish).
    holdout_select: bool = False
    # The reserved-holdout fraction the run committed to at start (None in old logs / when off).
    # The engine re-uses this on resume so the split every metric was scored against never changes.
    holdout_fraction: Optional[float] = None
    # R1-c (folded from run_started; False for old logs -> byte-identical legacy selection). When True,
    # best-selection's mean pick breaks a metric-EQUAL tie by the calibrated §12-verifier soundness score
    # (Node.verifier_score) — advisory, never overriding a strictly-better robust_metric (§21.7).
    select_verifier_tiebreak: bool = False
    # R1-d (§21.19): recorded `verifier_ci_tie` — widen the verifier tie-break to a statistical (CI) tie.
    # Folded from run_started; absent on old logs -> False -> byte-identical exact-tie selection.
    verifier_ci_tie: bool = False
    # Complete verifier treatment pinned by run_started so resume cannot mix sampling/criteria policies.
    select_verifier_samples: int = 3
    select_verifier_contract: str = "selection-criteria:v1"
    nodes: dict[int, Node] = Field(default_factory=dict)
    # Fold-internal current-failure threshold state. Keeping the causal crossing seq prevents a
    # reset/abort from regrouping old failures into a brand-new browser notification identity.
    current_failure_count: int = Field(default=0, exclude=True)
    failure_spike_level: int = Field(default=0, exclude=True)
    failure_spike_seq: Optional[int] = Field(default=None, exclude=True)
    # The node currently BEING BUILT (a `node_building` marker), shown in the UI the instant work starts
    # on it — before the dev session finishes with node_created. Transient: {node_id, operator,
    # parent_ids, started, optional bounded card_id}; cleared when that node's node_created/node_failed
    # folds. NOT in `nodes`, so it never affects id allocation (max(nodes)+1) or resume. None when no node
    # is mid-build.
    building: Optional[dict] = None
    # ALL nodes currently being built, keyed by node_id — the concurrent-build-width superset of the
    # singular `building` above (which stays the MOST-RECENT build, untouched, for back-compat). Each
    # value is the SAME transient marker shape
    # {node_id, operator, parent_ids, started, generation?, card_id?}.
    # Under concurrent builds the singular field holds only the last-appended `node_building`, so the UI
    # would render just one ghost; this collection lets it render every in-flight build. Empty when
    # nothing is mid-build and on old logs (default_factory). Like `building`, never in `nodes`, so id
    # allocation (max(nodes)+1) and resume are untouched.
    buildings: dict[int, dict] = Field(default_factory=dict)
    best_node_id: Optional[int] = None
    # Node ids the trust gate bars the search from BREEDING (improve/merge/ablate/confirm target) —
    # the hard-flagged (cheating/leaking) set under trust_gate=gate/block, stamped by the fold's
    # `_apply_trust_gate` post-pass. Under `gate` these stay `feasible` (kept in the tree for
    # diversity/audit) but are excluded from `breedable_nodes()`; empty under `audit` / old logs.
    breed_excluded: set[int] = Field(default_factory=set)
    finished: bool = False
    # Durable, opt-in finalization handshake. `last_finish_seq` is the currently accepted
    # run_finished. Modern engine finishes carry `finalization_required=true`; only their matching
    # finalization_finished marker advances `finalized_finish_seq`. Legacy markerless finishes are
    # treated as finalized by replay so old persisted runs never become synthetic recovery work.
    last_finish_seq: int = -1
    finalized_finish_seq: int = -1
    # Seq of the accepted finalization_finished marker (not the finish it names). Fold-internal and
    # excluded from API payloads; attention uses it to ignore duplicate/stale marker envelopes.
    finalization_marker_seq: Optional[int] = Field(default=None, exclude=True)
    data_profile: Optional[dict] = None   # set by the grounding pre-phase (I16)
    leakage: Optional[dict] = None        # set by the grounding leakage scan (I9)
    data_provenance: Optional[dict] = None  # D4: pinned content hashes of task assets/data
    # Out-of-process / host-side grading (B1+): when set, the candidate wrote only predictions and the
    # HOST scored them against held-out labels it never put on the candidate FS. {scorer, predictions,
    # n_labels} — the labels themselves NEVER enter the event log. Audit/UI only.
    host_grading: Optional[dict] = None
    stop_reason: Optional[str] = None     # why the run finished (budget/leakage/done)
    confirmed_done: bool = False          # the multi-seed confirmation phase completed (I12)
    # P0-2 search epoch: bumped when a FINISHED run is reopened (resume/run_reopened). The nodes
    # added after a reopen are a fresh candidate set, so the prior confirmation/approval COMPLETION
    # (confirmed_done/approved below) must not carry over — else a better new candidate can never be
    # confirmed (the confirm phase is skipped) or re-approved. Defaults 0; old logs stay at 0 and
    # fold byte-identically until an actual reopen-after-finish occurs.
    search_epoch: int = 0
    # P1-1 recoverable-intent kernel: seq of the last durable `resume_requested` (appended by /resume
    # before spawning the detached engine) and of the last engine-written `resume_served` (appended
    # once the engine holds the singleton lock). A request whose seq is NEWER than the last serve is an
    # UNFULFILLED resume — the engine crashed before running — which the on-load reconciler re-spawns.
    # `resume_pending()` reads these. `_ts` carries the request's event time so the reconciler can wait
    # a grace period before re-spawning. All 0 on old logs -> never pending -> unchanged behavior.
    last_resume_request_seq: int = 0
    last_resume_served_seq: int = 0
    last_resume_request_ts: float = 0.0
    # Which command a pending durable launch must run. A finalize request that arrives in the narrow
    # post-run_finished lock tail must remain a finalize hand-off, not be replayed as a normal resume
    # (which would reopen the search). Launch-claim records preserve this mode.
    last_resume_request_mode: str = "resume"
    # A `resume_requested` carrying launch_claim=True is the durable cross-process claim made
    # immediately before Popen. It prevents two uvicorn workers (or a post-exit waiter racing a new
    # request) from launching duplicate detached CLIs during the gap before engine.lock is acquired.
    # If the claimant itself dies, the timestamp expires and the reconciler may safely claim again.
    last_resume_launch_seq: int = 0
    last_resume_launch_ts: float = 0.0
    awaiting_approval: bool = False       # HITL: approval requested, not yet granted (I21)
    approved: bool = False                # HITL: a human approved the result (I21)
    # P0-2: the node id the pending approval request was raised for (folded from `approval_requested`),
    # audit-only — surfaced in the projection so the UI can show WHAT is awaiting approval. It does NOT
    # gate the grant: `_on_approval_granted` honors any grant that names a REAL node in the run (so an
    # operator may `approve --node-id N` a non-best node) and rejects a forged/unhashable/non-existent id.
    # None when no request is pending.
    approval_subject: Optional[int] = None
    approval_generation: Optional[int] = None   # lifecycle generation the pending request names
    approval_request_seq: Optional[int] = Field(default=None, exclude=True)
    approved_node_id: Optional[int] = None      # explicit human choice; overrides algorithmic best
    archive: Optional[dict] = None        # diversity-archive summary at run end (I22)
    # Breadth read-model recorded at the strategist cadence: the run's narrowing curve (themes,
    # niches, theme entropy, dominant-theme fraction). The folded field is not a selector input, but
    # a live Strategist may use it to change later search policy. Each entry carries `at_node` so the
    # emission gate is idempotent on resume. See search/coverage.py.
    coverage_snapshots: list[dict] = Field(default_factory=list)
    # PART IV Phase 2a: concept-graph coverage + uncovered-region snapshots (the "0 coverage in {X}"
    # pivot signal) recorded at the `concept_retag_every` cadence (via `_should_consult_concepts`, not
    # `strategist_every`) when `concept_pivot` is on. The folded field
    # does not directly select a winner, but the live Researcher cue can change future candidates.
    # Each entry carries `at_node` so the emission gate is idempotent on resume.
    # Additive/reader-defaulted: empty on old logs -> byte-identical fold. See search/concept_graph.py.
    concept_coverage_snapshots: list[dict] = Field(default_factory=list)
    # PART IV D5 (§21.16, Phase 2c): per-node concept memberships (node_id -> [concept_id]). A membership
    # may originate on the Researcher-authored Idea or from the independent `node_concepts` classifier
    # event; the last writer wins for read-model compatibility. Consumers that can affect admission MUST
    # consult node_concept_provenance rather than assuming every membership came from the classifier.
    node_concepts: dict[int, list[str]] = Field(default_factory=dict)
    # PART V (B): the RUN's BASE concept set — the common technologies every node uses unless a node
    # states otherwise (folded from `run_concepts` events). A node may then author only the DELTA vs this
    # base + its parents (see `node_concept_deltas`), keeping per-node annotations minimal. Additive /
    # reader-defaulted; empty on runs that never set a base (every node then authors its own full set).
    run_base_concepts: list[str] = Field(default_factory=list)
    # Derived integrity receipt for the bounded run base. ``None`` means its stored set is exact; a
    # partial/unavailable envelope disables modern delta authoring while preserving a bounded audit view.
    run_base_concept_receipt: Optional[ConceptMaterializationReceipt] = None
    # PART V (B): bounded per-node concept DELTAS {node_id -> {"added": [...], "removed": [...]}} authored
    # on the Idea when `concept_mode="delta"` (including an explicit pair of empty lists). Replay stores
    # the tolerant reader's bounded valid operands here; the append-only Event remains the lossless audit
    # source. A deterministic POST-PASS in `fold` materializes each such node's
    # effective `node_concepts` = inherited − removed + added, where inherited = the run BASE at a root, else
    # the UNION of the node's parents' effective sets (the base flows in through the roots and down the DAG,
    # so a removal propagates). Kept as a
    # topological read-time resolution (not folded in event order) so `fold` stays ORDER-TOLERANT
    # (invariant 5): the post-pass sees the complete DAG, so a spliced/reordered log resolves identically.
    node_concept_deltas: dict[int, dict] = Field(default_factory=dict)
    # partial/unavailable materialization is represented by a closed, ordered reason envelope.
    # An unresolved dependency (including every active descendant) materializes to [] fail-closed; bounded
    # identity loss keeps the valid subset. The receipt prevents either fallback from being presented as an
    # exact membership by ConceptFrame. Keys are current/historic node ids; current-state projections apply
    # the same tombstone/abort lifecycle filter as memberships.
    node_concept_materialization_receipts: dict[int, ConceptMaterializationReceipt] = Field(
        default_factory=dict)
    # proposer-authored taxonomy is an untrusted claim, never classifier evidence. This
    # replay-derived sidecar records the producer of the CURRENT last-write-wins membership. Missing and
    # unknown values are deliberately untrusted; legacy generation-zero `node_concepts` events replay as
    # `classifier`, while old `node_created` Idea.concepts replay as `researcher-authored` without migration.
    node_concept_provenance: dict[int, str] = Field(default_factory=dict)
    # PART IV D5 (§21.18 B1): the concept-graph vocabulary SIZE when each node was last tagged. A node
    # tagged against a much smaller vocabulary than the current one is STALE (a concept minted by a later
    # node may now apply to it), so the cadence re-tags the most-stale nodes against the grown vocabulary
    # (bounded per cadence). Additive/reader-defaulted; empty on old logs / pre-B1 events -> no re-tag.
    node_concepts_at_vocab: dict[int, int] = Field(default_factory=dict)
    # PART IV D4 (§21.18 HT): per-hypothesis agentic concept tags (hyp_id -> [concept_id]) recorded once by
    # the LLM tagger, reused by taxonomy dedup instead of the tag_text alias heuristic. Populated only when
    # `concept_pivot` is on. Advisory rather than pure telemetry: taxonomy dedup and concept cadences reuse
    # these tags, so they can steer later board consolidation; they never directly re-rank evaluated nodes.
    # Additive/reader-defaulted: empty on old logs -> byte-identical fold.
    hypothesis_concepts: dict[str, list[str]] = Field(default_factory=dict)
    # PART IV D4 (§21.18 B1-ext): concept-graph vocabulary SIZE when each hypothesis was tagged — a
    # hypothesis tagged against a much smaller vocabulary is STALE and gets re-tagged against the grown one
    # (bounded per cadence), mirroring node_concepts_at_vocab. Additive/reader-defaulted; empty on old logs.
    hypothesis_concepts_at_vocab: dict[str, int] = Field(default_factory=dict)
    # PART IV D5 (§21.18 B3): the accumulated concept-consolidation rename map (raw_id -> canonical_id).
    # Reused by later cadences so consolidation decisions stay FIXED (stable vocabulary, no flapping / B1
    # churn). Populated only when `concept_pivot` is on. The map canonicalizes materialized memberships
    # and therefore can change later coverage/proposal cues; it never directly re-ranks evaluated nodes.
    # Additive/reader-defaulted: empty on old logs -> byte-identical fold.
    concept_consolidation: dict[str, str] = Field(default_factory=dict)
    # PART IV concept-edge substrate: the typed concept graph (src, rel, dst) -> {provenance, confidence},
    # keyed by "src\trel\tdst". Makes hierarchy a swappable projection (project_hierarchy). Folded
    # COMMUTATIVELY from explicit EV_CONCEPT_EDGE assertions (max-confidence-wins per triple ->
    # order-tolerant). Derived ``co_occurs`` rows are intentionally omitted and recomputed from current
    # memberships by ConceptFrame, so stale counts can decrease/disappear. Advisory: hierarchy and
    # coverage views can feed later strategy/proposal cues, but the edges never directly re-rank evaluated
    # nodes. Additive/reader-defaulted: empty on old logs -> path projection remains available.
    concept_edges: dict[str, dict] = Field(default_factory=dict)
    # RepoTask onboarding (Phase 3, ADR-7): the agent proposes a trusted eval spec + metric
    # adapter; a human ratifies it once; then the loop trusts it.
    proposed_spec: Optional[dict] = None  # {eval_spec, adapter_files, goal} from the agent
    spec_approval_requested: bool = False
    spec_approval_request_seq: Optional[int] = Field(default=None, exclude=True)
    spec_confirmed: bool = False          # human ratified the proposed eval spec
    # Drift cross-check audit (Phase 4, ratify_freeze_drift): each entry is a divergence the
    # independent reader caught {node_id, primary, cross, tolerance, [seed]}. Audit only —
    # the metric was already discarded (node failed), so this never changes selection.
    drifts: list[dict] = Field(default_factory=list)
    # Workspace reproducibility (item #4): the editable-repo/data fingerprint pinned at
    # run_started, and whether a resume detected the source changed underneath.
    workspace: Optional[dict] = None
    workspace_changed: bool = False
    # F18: folded like workspace_changed so the env-drift note is emitted ONCE, not re-appended on
    # every resume of an upgraded run (the emit is gated on `not state.env_changed`).
    env_changed: bool = False
    # P0-5 environment identity: the Python/platform + key-library version fingerprint pinned at
    # run_started. A resume compares the current environment against it and emits `env_changed` (a
    # diagnostic) on drift — a run continued after a library upgrade is no longer bit-reproducible, so
    # record it instead of pretending it is. None on old logs -> no env pin -> the check is skipped.
    env: Optional[dict] = None
    # P0-5 dirty-input enumeration: for a repo task, the list of workspace files that were UNCOMMITTED
    # (git status --porcelain) at run start — the explicit "which inputs differ from a clean checkout"
    # on top of the content hash the workspace fingerprint already pins. Empty for non-repo/clean runs
    # and old logs. Provenance only; never gates.
    dirty_inputs: list[dict] = Field(default_factory=list)
    # Eval-compute budget accounting (#2): cumulative wall-clock spent INSIDE evals (training
    # runs), distinct from the run's total wall-clock (which includes LLM/agent time). The
    # search stops cleanly once this crosses `max_eval_seconds` — guards the silent long sweep.
    total_eval_seconds: float = 0.0
    # P1-2 separate budget buckets: the SAME cumulative eval seconds split by category (node/search
    # eval vs multi-seed confirm) for observability — where the compute went, not just the total. LLM
    # spend is already its own bucket (llm_cost -> total_llm_*); holdout re-scores existing predictions
    # for free (no eval_seconds), so it never contributes. Sums to total_eval_seconds. Empty on old
    # logs -> populated additively on the next fold; never gates selection.
    eval_seconds_by_kind: dict[str, float] = Field(default_factory=dict)
    # Per-seed confirmation results {node_id: {seed: metric|None}} from `confirm_eval` events —
    # lets a crash-interrupted confirm pass RESUME mid-node (skip seeds already run) instead of
    # re-executing every expensive full-profile seed from scratch.
    confirm_seed_results: dict[int, dict] = Field(default_factory=dict)
    # D1: every node that received a `holdout_evaluated` event (even with a null metric — e.g.
    # its predictions file was gone). The replay-safe gate that stops the holdout phase from
    # re-attempting a node forever on resume.
    holdout_evaluated_ids: list[int] = Field(default_factory=list)
    # Whether the CURRENTLY-disclosed holdout was recorded with epoch semantics (a modern
    # holdout_evaluated stamps `search_epoch`; a legacy one does not). Derived during fold, not
    # persisted. Gates the metric-wiping requeue when a later candidate change re-hides the split:
    # legacy holdout logs predate search epochs and must NOT wipe surviving incumbents on replay
    # (invariant 5b — old logs fold as before). Default False = legacy-safe for old logs.
    holdout_epoch_aware: bool = False

    # --- live operator control (UI intervention via the event log) ---
    # These are folded from authenticated, allow-listed CONTROL events (intent) appended by server/CLI
    # writers through EventStore serialization. The engine owns their domain effects (e.g. node_abort ->
    # node_failed reason="aborted"). All deterministic under replay; fields that are only receipts do
    # not directly change best-selection.
    paused: bool = False                       # `pause`/`resume`: resumable break (not finished)
    pause_node_id: Optional[int] = None         # scoped auto-pause owner (None = explicit operator pause)
    pause_generation: Optional[int] = None
    pause_event_seq: Optional[int] = Field(default=None, exclude=True)
    stop_requested: Optional[str] = None       # `run_abort`: reason; loop -> run_finished + break
    # Seq of the latest finalize intent. A request newer than the accepted finish still needs a new
    # finish/finalization boundary; an older one was already consumed by that finish.
    last_stop_request_seq: int = -1
    aborted_nodes: list[int] = Field(default_factory=list)   # `node_abort`: skip/kill these nodes
    budget_overrides: dict = Field(default_factory=dict)     # `budget_extend`: max_seconds/eval
    pending_hints: list[dict] = Field(default_factory=list)  # `hint`: operator directives to steer
    confirm_requests: list[int] = Field(default_factory=list)  # `force_confirm`: operator robustness ask
    confirmed_forced: list[int] = Field(default_factory=list)   # nodes a forced confirm finished (gate)
    # Generation-aware twins keep reset lifecycles distinct while the id-only lists above preserve the
    # existing UI projection/backward-compatible surface.
    confirm_request_generations: list[dict] = Field(default_factory=list)
    confirmed_forced_generations: list[dict] = Field(default_factory=list)
    ablate_requests: list[int] = Field(default_factory=list)    # `force_ablate` (wired in Phase 5)
    ablate_request_generations: list[dict] = Field(default_factory=list)
    fork_requests: list[dict] = Field(default_factory=list)     # `fork`: operator-seeded improve
    # CURSOR into `fork_requests`, not a count: a receipt names the position it completed and the
    # fold advances through it, clamped to the queue (`replay._advance_request_cursor`).
    forks_done: int = 0
    # Layer 5's durable main-task Card producer gate. Hidden because this is execution/recovery state,
    # not a public board payload; old logs therefore keep the exact serialized RunState shape.
    card_build_requests: list[dict] = Field(default_factory=list, exclude=True)
    card_builds_done: int = Field(default=0, exclude=True)
    # Paid-attempt receipts (`card_build_attempted`) per request identity, as `{card_id, generation}`
    # rows in append order. The request above is the LOGICAL gate ("build this Card"); this is the
    # PHYSICAL one ("a producer was started, so a provider call may already be paid for"). A head that
    # carries an attempt from a dead process is quarantined rather than silently re-issued.
    card_build_attempts: list[dict] = Field(default_factory=list, exclude=True)
    # One normalized outcome for every replay-accepted positional Card-build completion.  The
    # quality gate needs the complete denominator: looking only at ``speculative_nodes`` would hide
    # pre-commit stale/producer failures and could turn 99 misses + 1 hit into a reported 100% hit
    # rate.  Hidden like the request queue so the public/legacy RunState projection is unchanged.
    card_build_outcomes: list[str] = Field(default_factory=list, exclude=True)
    # Only replay-accepted exact-head give-ups enter this hidden set-like list. Runtime scheduling must
    # never infer serial fallback from raw/orphan ``card_build_done`` rows that fold deliberately rejects.
    card_build_producer_failed: list[str] = Field(default_factory=list, exclude=True)
    # node_id -> exact request identity reconstructed from a successful card_build_done. Consumers use
    # this durable link (never merely Idea.card_id) to count and freshness-check speculative work.
    speculative_nodes: dict[int, dict] = Field(default_factory=dict, exclude=True)
    # `inject_node`: an operator-authored experiment hand-added to the tree (a manual idea +
    # optional parent + optional code). The engine materializes each one into a real pending node
    # that the policy then evaluates like any other — so a human can steer the search directly.
    inject_requests: list[dict] = Field(default_factory=list)
    injects_done: int = 0      # cursor into `inject_requests`; same rule as `forks_done` above
    annotations: dict[int, list[str]] = Field(default_factory=dict)  # legacy `annotation`: node notes
    # Modern collaboration is read only through authenticated, bounded projections.  Excluding it
    # here prevents free-form comment text from entering the tokenless /state + SSE payload.
    comments: dict[str, CommentState] = Field(default_factory=dict, exclude=True)
    # Safe scalar used by clients to refresh the bounded comment projection only when it changes.
    comments_revision: int = -1
    promotions: list[dict] = Field(default_factory=list)        # `promote`: solution-registry audit
    champion: Optional[int] = None             # node id the `champion` registry alias points at
    llm_cost: Optional[dict] = None            # run-level LLM cost/token roll-up ({cost,tokens,…})
    ablations: list[dict] = Field(default_factory=list)  # ablate events {parent_id, impacts} (sensitivity)
    policy_scores: dict[int, float] = Field(default_factory=dict)  # latest policy_decision candidate scores
    policy_chosen: Optional[int] = None                  # node the policy expanded ("why this node")
    policy_reason: str = ""                               # short why-this-node label (exploit/merge/promote/…)
    # A7 Strategist replay control. `active_strategy` is the latest applied Strategy dict and is
    # re-applied on engine entry; `strategy_history` is the timeline of switches for the "why this
    # strategy" panel. `pending_strategy` is an operator override (set_strategy control event) that
    # the engine applies before consulting the Strategist (human-wins parity with pause/hint).
    active_strategy: Optional[dict] = None
    strategy_history: list[dict] = Field(default_factory=list)
    pending_strategy: Optional[dict] = None
    # A1 ASHA: rung-promotion audit trail {rung, survivors} for the UI (successive-halving view).
    rungs: list[dict] = Field(default_factory=list)
    # --- advisory/control receipts (no direct objective ranking; downstream effects noted per field) ---
    # Advisory-vs-behavior audit (reconciled 2026-08-08): comments and docstrings now distinguish
    # "never directly re-ranks the metric champion" from "pure telemetry". A folded receipt may satisfy
    # the first claim while still feeding prompts, cadence gates, Card selection, or trust enforcement.
    # `novelty_grades` is behavioral admission/steering; `agent_decisions` and `cross_run_priors` are
    # observational here; `report` is selection-neutral but its receipt gates regeneration cadence.
    # Unified self-driving agent (audit-only; never read by best-selection): timeline of the agent's
    # macro-action choices {at_node, chosen, legal, recommended, rationale} for the "why this action"
    # view. Additive — old event logs without `agent_decision` events fold to an empty list.
    agent_decisions: list[dict] = Field(default_factory=list)
    # A6 proxy/predictive scoring: per-node early-signal scores + which candidates were skipped.
    proxy_scores: dict[int, float] = Field(default_factory=dict)
    proxy_skipped: list[int] = Field(default_factory=list)
    # B5 reward-hacking detector: flagged suspicious wins {node_id, signals:[{signal, detail}]} for
    # the Trust panel. Under trust_gate=audit they are advisory; hard signals under gate/block exclude
    # best-selection/breeding and may mark the node infeasible.
    reward_hacks: list[dict] = Field(default_factory=list)
    # FOREAGENT predict-before-execute picks {node_id, confidence, chosen, ...}, folded from
    # `foresight_selected` events. They do not re-rank evaluated nodes; the fold keeps them so the
    # world model can be primed with its OWN track record (did past predicted-best picks beat their
    # parent?), which can change later pre-execution picks (signal-delivery, §1). Additive.
    foresight_selected: list[dict] = Field(default_factory=list)
    # E1 novelty/dedup gate: near-duplicate proposals that were nudged off {node_id, near_node, ...}.
    novelty_events: list[dict] = Field(default_factory=list)
    # PART IV D3 (Phase 2b): the live gate's GRADED-ALLOW decisions — proposals allowed despite a
    # concept overlap the flat gate would reject (level-4 same-direction-new-impl, level-5 re-open of a
    # wrongly-abandoned direction). Never re-ranks evaluated nodes / the metric champion, but NOT pure
    # telemetry: `_derive_cards` folds it (with `novelty_events`) onto `card.novelty_verdict`, and when
    # card_driven_selection is enabled that feeds the card novelty signal
    # (search/card_selection.py::_novelty_signal) steering WHICH candidate is built next. Additive.
    novelty_grades: list[dict] = Field(default_factory=list)
    # PART IV cross-run Step 2 (§21.20): concepts the proposed idea shares with a SIMILAR earlier run,
    # surfaced (never rejected) so the trace/researcher sees "tried in run X -> metric Y". Populated only
    # under `cross_run_concepts`; audit-only sidecar, never read by best-selection. Additive.
    cross_run_priors: list[dict] = Field(default_factory=list)
    # Deep-Research stage (Phase 2). `research` does not directly rank evaluated nodes, but its latest
    # memo is proposal context and verified claims may feed cross-run evidence. `research_requests` are pending
    # manual `deep_research` control events and `research_served` how many have been fulfilled (the
    # replay-safe gate, mirroring inject_requests/injects_done).
    research: list[dict] = Field(default_factory=list)
    research_requests: list[dict] = Field(default_factory=list)
    research_served: int = 0
    # Paid-attempt receipts for the Deep-Research stage (`research_attempted`), each
    # `{attempt_id, trigger, at_node, manual}`. The gates read attempts as well as memos, so a kill
    # between "the provider answered" and "the memo is durable" does NOT re-spend on resume; the
    # trigger is simply spent. `research_attempts_completed` holds the ids whose memo DID land, so an
    # attempt is only counted as outstanding while it really is. Hidden: recovery bookkeeping, and
    # keeping them out of the dump preserves the public state contract for old and new logs alike.
    research_attempts: list[dict] = Field(default_factory=list, exclude=True)
    research_attempts_completed: set[str] = Field(default_factory=set, exclude=True)
    # The former core hypothesis board (`hypotheses: dict[str, Hypothesis]`) was removed after Card became
    # the canonical work-item/evidence model. Core consumers now read `research_cards()` and the explicit
    # belief views; `_derive_cards` folds the raw accumulators below directly. The public server preserves
    # a deprecated read-only `hypotheses` compatibility projection derived from bounded Card DTOs during
    # the post-L6 migration window (`serve/appstate.py`). The accumulators stay as frozen event inputs;
    # only the duplicate core model/class are gone.
    # Explicitly-added hypotheses (human `add_hypothesis` control event or a deep-research direction),
    # kept separately so the derived-from-nodes pass can merge evidence into them. `abandoned` ids are
    # a human/agent override of the derived status.
    hypotheses_added: list[dict] = Field(default_factory=list)
    # P1+ agentic merge: `hypothesis_merged` events fold ALIAS hypotheses (paraphrases the exact-hash
    # ledger kept separate) into a CANONICAL id, deterministically applied in `_derive_cards`.
    hypotheses_merged: list[dict] = Field(default_factory=list)
    hypotheses_abandoned: list[str] = Field(default_factory=list)
    # Human-DELETED legacy/pure-belief rows (`hypothesis_updated status=deleted`) are removed from the
    # Card projection entirely, unlike `abandoned` (which remains visible). Native work-item Card ids are
    # separate; this compatibility journal addresses the legacy statement-hash identity lane.
    hypotheses_deleted: list[str] = Field(default_factory=list)
    # FOREAGENT board prioritization. The latest `hypothesis_ranked` event carries
    # {at_node, order:[ids], confidence, reason, ranked:[{id,statement}]}. It never re-ranks evaluated
    # nodes, but orders open hypotheses for later proposal context and is the compatibility fallback
    # for Card priority; native writers publish the corresponding card_ranked event atomically.
    hypothesis_ranking: Optional[dict] = None
    # Card ledger derived each fold by `_derive_cards`, keyed by card id. It never directly chooses the
    # metric champion; when card_driven_selection is enabled, the engine consumes selection-ready rows
    # to choose which candidate action to build next.
    cards: dict[str, Card] = Field(default_factory=dict)
    # Folded inputs for `_derive_cards` (mirror the `hypotheses_*` lists). These are canonical bounded
    # replay receipts, never raw Event.data: `cards_added` keeps the thin action seed, `cards_merged` the
    # alias->canonical identity edges, and `cards_dropped` only {id, reason, dropped_by}.
    cards_added: list[dict] = Field(default_factory=list)
    cards_merged: list[dict] = Field(default_factory=list)
    cards_dropped: list[dict] = Field(default_factory=list)
    # Layer 1b enrichment channel. `cards_enriched`: engine/operator card_enriched deltas (novelty verdict,
    # cross-run prior, footprint-finalize, steering cues), applied last-write-by-seq in `_derive_cards`.
    # `card_ranking`: the latest `card_ranked` event {order:[card ids], confidence, reason}; stamps each
    # open card's `priority` (falls back to `hypothesis_ranking` while the engine still ranks hypotheses).
    cards_enriched: list[dict] = Field(default_factory=list)
    card_ranking: Optional[dict] = None
    # Operator-override maps filled by Card control events. `_derive_cards`
    # overlays them in a FIXED LAST phase so the operator always wins regardless of event arrival order
    # (docs/23 decision 27). Empty {} in Layer 1 -> the overlay is a no-op; reserving them now means Layer
    # 6 needs no `_derive_cards` rewrite. Keyed by card id.
    card_priority_pins: dict[str, int] = Field(default_factory=dict)
    card_operator_edits: dict[str, dict] = Field(default_factory=dict)
    card_resource_pins: dict[str, dict] = Field(default_factory=dict)
    # Agent-authored run report (selection-neutral narrative; never read by best-selection).
    # The latest `report_generated` event's content is also the replay-safe regeneration-cadence receipt.
    # The UI renders deterministic node analysis and layers this narrative on top.
    report: Optional[dict] = None
    # M6 comparative-lesson replay receipts. They do not directly rank evaluated nodes, but they gate
    # paid cadence work and the shared lesson store subsequently feeds proposal prompts.
    # `lessons_distilled` records each mid-run distillation (at_node + the (child, parent) node-id
    # pairs spent + the statements) — it is BOTH the replay-safe cadence gate and the ledger that
    # stops a later firing (or run-end reflection) from re-distilling the same pair.
    # `lessons_refreshed` records each mid-run re-read of the shared cross-run store (cadence gate).
    lessons_distilled: list[dict] = Field(default_factory=list)
    lessons_refreshed: list[dict] = Field(default_factory=list)

    @field_serializer("run_setup_done")
    def _ser_run_setup_done(self, v: set) -> list:
        # Serialize the str-set as a SORTED list so the projection is deterministic across processes
        # (final ultra-review §A): a plain set[str] dumps in hash-slot order, which PYTHONHASHSEED
        # randomizes per process (unlike set[int], whose hash==value), so `looplab replay` / `/state`
        # could show a spurious ordering diff for a run with ≥2 distinct run_setup commands. The live
        # attribute stays a set (membership is all the fold/engine use); only the dump is ordered.
        return sorted(v)

    # --- read helpers (no mutation) ---
    def resume_pending(self) -> bool:
        """P1-1: a durable resume intent was recorded but no engine has served it yet (its request seq
        is newer than the last serve). Combined by the reconciler with a not-alive / not-finished probe
        to detect a zombie whose resume spawn died before the engine ran."""
        return self.last_resume_request_seq > self.last_resume_served_seq

    def finalization_pending(self) -> bool:
        return (self.finished and self.last_finish_seq >= 0
                and self.finalized_finish_seq != self.last_finish_seq)

    def best(self) -> Optional[Node]:
        return self.nodes.get(self.best_node_id) if self.best_node_id is not None else None

    def research_cards(self) -> list["Card"]:
        """Canonical (non-merged-away) Card work items on the research board.

        Cards carry the former hypothesis-facing fields, but work-item identity and belief identity are
        deliberately separate: multiple cards may share one ``belief_id`` (for example a debug retry).
        Consumers that need one row per research question use ``open_research_beliefs`` or
        ``events.belief_projection.grouped_beliefs``. Merged aliases are already collapsed out of
        ``self.cards``; this only drops any lingering ``merged_into`` row for safety.
        """
        return [c for c in self.cards.values() if c.merged_into is None]

    def open_research_cards(self) -> list["Card"]:
        """Research cards still OPEN for evidence (verdict 'open', not dropped) — the card equivalent of
        the old `open` hypotheses fed to proposal prompts and foresight ranking. The blank-statement
        guard mirrors the fold's `_derive_hypotheses` empty-skip: the old board never rendered a
        statement-less bullet, so a malformed card_added with an empty seed must not surface one here."""
        return [c for c in self.research_cards()
                if c.verdict == "open" and c.status != "dropped" and c.seed_statement.strip()]

    def open_research_beliefs(self) -> list["Card"]:
        """The open, UNTESTED research board as distinct BELIEFS (peer review): the
        `[open_research_cards() with no evidence yet]` list the Researcher proposal feed and foresight
        ranking consume, collapsed by seed-statement DIGEST (the `grouped_beliefs` key) so two work-item
        cards that reuse the exact hypothesis wording surface as ONE belief — not indistinguishable
        duplicates the model re-reads and re-ranks. The FIRST-seen no-evidence card per belief is the
        representative (deterministic over `self.cards` insertion order); its distinct work-item id is
        preserved for the caller's evidence joins. `grouped_beliefs` remains the FULL-board view that
        aggregates evidence + verdict across a belief's cards.

        The key is `Card.belief_id`, published by the fold (`card_ledger.py::_apply_card_belief_lineage`)
        so this method and `grouped_beliefs` provably group on ONE spelling; the inline fallback covers a
        hand-assembled `RunState` that never went through `fold`."""
        seen: set[str] = set()
        out: list["Card"] = []
        for c in self.open_research_cards():
            if c.evidence:                  # untested only (mirrors the consumers' `if not c.evidence`)
                continue
            key = c.belief_id or hypothesis_statement_digest(c.seed_statement)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out

    def evaluated_nodes(self) -> list[Node]:
        # `not n.tombstoned` gates ALL downstream selection at the source: feasible_nodes/
        # breedable_nodes and the best-pick post-pass all read through here, so a logically-deleted
        # node can never be selected best, bred from, or confirmed. (§6.3 append-only delete.)
        return [n for n in self.nodes.values()
                if n.status is NodeStatus.evaluated and not n.tombstoned]

    def feasible_nodes(self) -> list[Node]:
        """Evaluated nodes that satisfied all hard constraints (#5). These are the only nodes
        eligible to be selected as best or bred from — a constraint-violating node keeps its
        metric for the audit trail but never drives the search forward. A node with no metric
        (tolerated from a hand-edited/BYO-script log by replay) is excluded too: it can neither be
        sorted against real metrics nor selected as best, and would raise TypeError in the policies'
        metric-keyed sorts."""
        return [n for n in self.evaluated_nodes()
                if n.feasible and is_usable_metric(n.metric) and n.id not in self.aborted_nodes]

    def breedable_nodes(self) -> list[Node]:
        """Feasible nodes the search may BREED FROM or CONFIRM (improve/merge/ablate/promote/confirm
        target). Under `trust_gate=gate` a hard-flagged (cheating/leaking) node stays feasible — kept
        in the tree for diversity/audit and barred from WINNING elsewhere — but is NOT bred from, so
        the search never sinks budget improving a cheating lineage or displaces an honest node from
        the confirm top-k (T2, §2.2). Under `block` it is already infeasible (out of feasible_nodes).
        `audit` / no flags -> identical to feasible_nodes(); the fast path keeps it a no-op there."""
        if not self.breed_excluded:
            return self.feasible_nodes()
        return [n for n in self.feasible_nodes() if n.id not in self.breed_excluded]

    def pending_nodes(self) -> list[Node]:
        # A tombstoned pending node (its subtree was logically deleted while it was still queued)
        # must NOT be handed back to the eval loop on resume — skip it here too (§6.3).
        return sorted(
            (n for n in self.nodes.values()
             if n.status is NodeStatus.pending and not n.tombstoned),
            key=lambda n: n.id,
        )

    def is_better(self, a: float, b: float) -> bool:
        # Delegates to the single comparator owner (core/fitness.py) so "better" has ONE spelling
        # across the fold, the policies and this convenience primitive (R1/SearchFitness).
        return _is_better(self.direction, a, b)


# -------------------------------------------------- the Card lane's "did this node spend budget"
# These FOUR lived in `search/card_selection.py` until 2026-08-05 and are re-exported from there
# (the SAME objects, so every existing import site and patch seam still resolves to one definition).
# They moved DOWN to core because the FOLD needs them: `events/replay.py`'s Card anchor gate must not
# treat a node the Card lane's own policy view cannot see as a child of the node it was going to
# debug, and `events` may not import `search` (CLAUDE.md layering). Duplicating the predicate
# replay-side would have made "did this node spend budget" answerable two ways, which is the exact
# class of disagreement the original defect was: replay counted the node, the Card lane's policy view
# did not, and the lane re-authored a permanently unselectable Card every loop turn. One definition,
# one answer.
#
# The discard predicate moved first (2026-08-05, commit 5620d11f) and that was the mistake worth
# naming: it moved ONE of the four classes `node_counts_toward_card_budget` hides, so the fold went
# on disagreeing about the other three (`tombstoned`, `feasible=False`, `breed_excluded`) and the
# identical runaway reopened on those axes within a day. The unit that has to be shared is the
# PREDICATE, not the leaf of its proof — so `node_counts_toward_card_budget` itself now lives here.


CARD_FRESHNESS_SUPERSEDED_ERROR = "superseded by Card freshness gate"


def _durable_speculative_lifecycle(state: "RunState", node: Node) -> bool:
    """Whether BOTH durable Layer-5 receipts bind this exact attempt-zero lifecycle to its Card.

    The Node's own ``speculative``/``card_build_generation`` marker (from ``node_created``) and the
    matching committed ``card_build_done`` link (``state.speculative_nodes``) must agree on the Card
    id AND the request epoch.  Either receipt alone is forgeable by an unrelated build of the same
    Card; together they name one producer result.
    """

    link = getattr(state, "speculative_nodes", {}).get(node.id)
    generation = getattr(node, "card_build_generation", None)
    return bool(
        node.attempt == 0
        and getattr(node, "speculative", False) is True
        and isinstance(link, Mapping)
        and link.get("card_id") == node.idea.card_id
        and type(generation) is int
        and type(link.get("generation")) is int
        and link.get("generation") == generation
    )


def is_unevaluated_speculative_discard(state: "RunState", node: Node) -> bool:
    """Prove the Layer-5 budget refund for a speculative build that NEVER RAN, from folded receipts.

    A speculative build that turns out not to match the next selection is thrown away before it is
    ever dispatched: it costs exactly one Developer call and touches no sandbox/GPU.  Charging it a
    node-budget slot is budget THEFT from the experiments the run still has to execute, so the slot
    is refunded.  A speculative node that DID consume an evaluation is a real experiment and keeps
    its slot, whatever its outcome.

    "Never ran" is proven, never inferred.  Four independent durable facts must agree:

    * both speculative receipts bind this attempt-zero lifecycle to one committed producer result
      (``_durable_speculative_lifecycle``) — an ordinary node can therefore never be refunded;
    * the node's creator PROMISED a durable eval-start boundary (``Node.eval_start_boundary``, from
      ``node_created``) and no such boundary was ever appended (``Node.eval_started`` is False).
      This pair is the load-bearing one, and it is why the promise exists at all: without it the
      absence of a boundary is not evidence, merely silence — a log written before the boundary
      existed says exactly the same nothing about a node whose sandbox ran for forty minutes before
      the process was killed.  FAIL CLOSED: no promise, no refund;
    * the terminal itself carries the writer's pre-dispatch marker ``Node.never_evaluated`` (the
      additive ``node_failed`` field), OR the exact zero-cost freshness receipt that was the original
      narrow refund;
    * the folded execution evidence CORROBORATES it: zero charged eval seconds and no
      ``stage_finished`` row.  Any evidence that the sandbox actually started outvotes the marker.
      (Kept, and it got STRONGER on 2026-08-07 without moving: it used to be the WEAK half because
      ``stage_finished`` was written inside the terminal's own write-lock block and cost is charged
      only by a terminal, so a killed evaluation left neither and this pair could never see an
      interrupted eval.  ``engine/evaluate.py`` now appends the stage rows once per ATTEMPT, inside
      the loop — a process killed mid-pipeline can leave stage rows with no terminal, and those rows
      now VETO a refund.  Strictly fail-closed: it only ever removes refunds, never grants one, and
      the load-bearing pair above is untouched — a build discarded before dispatch never enters
      ``_evaluate`` at all, so it has no stage row under either writer.
      It still also catches a writer that stamps the marker on a node the log shows really ran.)

    Deliberately NOT keyed on ``reason='superseded'`` alone: ordinary build/reset races use the same
    reason and remain charged.  Absence of a node workdir on disk is not evidence at all — replay
    cannot see the filesystem, and the refund must be a pure function of the event log.
    """

    return bool(
        node.status is NodeStatus.failed
        and _durable_speculative_lifecycle(state, node)
        and getattr(node, "eval_start_boundary", False) is True
        and getattr(node, "eval_started", False) is not True
        and node.eval_seconds == 0
        and not getattr(node, "stages", None)
        and (
            getattr(node, "never_evaluated", False) is True
            or (
                node.error_reason == "superseded"
                and node.error == CARD_FRESHNESS_SUPERSEDED_ERROR
            )
        )
    )


def node_counts_toward_card_budget(state: "RunState", node: Node) -> bool:
    """Whether a node consumes the L3 creation budget.

    Tombstones and both kinds of current gate exclusion do not steal future search capacity:
    constraint-gated nodes have ``feasible=False`` and trust-gated nodes are in ``breed_excluded``.
    Failed and aborted attempts still count unless separately tombstoned; they consumed a real build.
    The Layer-5 refund is a speculative build proven to have been discarded BEFORE it consumed any
    evaluation — this is the single place that answers "did this node spend budget", and the physical
    reservation ceiling (``Engine._hard_node_reservation_limit``) reads the same predicate through
    ``refunded_card_budget_node_ids`` so the two halves of the budget cannot disagree.

    It also answers a SECOND question, and that is why it lives in ``core`` rather than in
    ``search/card_selection.py`` where it was written: it defines the Card lane's whole node
    UNIVERSE.  ``card_selection._effective_policy_state`` builds the state the policy sees by
    filtering ``state.nodes`` through exactly this predicate, so "did this node spend budget" and
    "can the policy still see this node" are one fact by construction.  The fold's debug anchor
    (``events/replay.py::_card_debug_leaf_children``) has to answer that same question — a child the
    policy cannot see does not end its failed parent's life as a debuggable leaf — and ``events`` may
    not import ``search``.  A replay-side copy is how the two views came to disagree twice: first
    about a discarded prefetch, then about a tombstoned / constraint-gated / trust-gated child.
    Changing this predicate therefore moves the budget, the policy's universe and the fold's leaf
    test TOGETHER, which is the property that was missing, not an accident to be factored apart.
    """

    return (
        not node.tombstoned
        and node.feasible
        and node.id not in state.breed_excluded
        and not is_unevaluated_speculative_discard(state, node)
    )
