"""Domain models + event envelope (I0). Pydantic v2; JSON Schemas derive from these."""
from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import (BaseModel, ConfigDict, Field, field_serializer, field_validator,
                      model_serializer, model_validator)

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
    # CODEX AGENT: only the exact reviewed producer is evidence; proposer-authored labels remain display-only.
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
    # CODEX AGENT: explicit-but-unknown is not legacy. Treating it like an absent legacy field would
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
    # and audit-only — never affects search/selection; the UI groups nodes by it. Flows through the
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
    # CODEX AGENT: never infer this choice from list truthiness or serializer field presence — either
    # collapses an explicit zero delta into an absent legacy membership.
    # CODEX AGENT: this is the tolerant durable reader. Absent is distinct from authoritative full+[];
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
    # help", "a deeper tree overfits here"). Optional and audit-only — it turns the search from
    # "propose the next mutation" into "run experiments that resolve open questions". When set, the
    # fold derives/links a Hypothesis (id = slug of the statement) and tracks it to a verdict from the
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
        # CODEX AGENT: card linkage is advisory. A future/corrupt scalar must not reject node_created and
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
        # CODEX AGENT: the tolerant reader heals old logs, but a modern writer must retry malformed ids.
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


def durable_idea_payload(idea: Idea) -> dict[str, Any]:
    """Serialize an Idea for ``node_created`` without inventing a concept mode.

    The explicit pops are a regression-proof durable boundary in addition to Idea's nested serializer.
    Pydantic materializes list defaults during validation, so keep an explicitly supplied empty legacy
    field but do not turn a genuinely absent concept envelope into three authored empty lists.
    """
    payload = idea.model_dump(mode="json")
    if idea.concept_mode is None:
        payload.pop("concept_mode", None)
        for field in ("concepts", "concepts_added", "concepts_removed"):
            if field not in idea.model_fields_set:
                payload.pop(field, None)
    return payload


_RESOURCE_INT_MAX = (1 << 31) - 1


def _resource_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    else:
        return None
    return number if 0 <= number <= _RESOURCE_INT_MAX else None


def normalize_researcher_footprint(value) -> dict | None:
    """Tolerant durable reader for the researcher-owned quantitative resource declaration."""
    if not isinstance(value, dict):
        return None
    out: dict[str, int | None] = {}
    if "gpus" in value and (gpus := _resource_int(value.get("gpus"))) is not None:
        out["gpus"] = gpus
    if "gpu_mem_mib" in value:
        raw_mem = value.get("gpu_mem_mib")
        if raw_mem is None:
            out["gpu_mem_mib"] = None
        elif (memory := _resource_int(raw_mem)) is not None:
            out["gpu_mem_mib"] = memory
    # Authority fields (`pinned_by`/`finalized_by`) belong to later operator/developer events. Dropping
    # every non-quantitative key prevents a researcher-authored Idea from forging that provenance.
    return out or None


def valid_researcher_footprint(value) -> bool:
    if not isinstance(value, dict) or not value or not set(value) <= {"gpus", "gpu_mem_mib"}:
        return False
    if "gpus" in value and (type(value["gpus"]) is not int
                            or not 0 <= value["gpus"] <= _RESOURCE_INT_MAX):
        return False
    raw_mem = value.get("gpu_mem_mib")
    if ("gpu_mem_mib" in value and raw_mem is not None
            and (type(raw_mem) is not int or not 0 <= raw_mem <= _RESOURCE_INT_MAX)):
        return False
    return True


DEVELOPER_FOOTPRINT_MARKER = "# LOOPLAB_FOOTPRINT:"


def developer_artifact_footprint(proposed, code="", files=None) -> dict | None:
    """Resolve the Developer's quantitative finalization from its shipped artifact.

    An unspecified Researcher declaration deliberately stays unspecified for legacy scheduling. When
    resources were proposed, a Developer may confirm or scale them by placing one compact JSON marker in
    shipped code; absent/malformed markers conservatively retain the proposal. Only the two quantitative
    keys cross this boundary, so code comments cannot forge provenance.
    """
    fallback = normalize_researcher_footprint(proposed)
    if fallback is None:
        return None
    blobs: list[str] = [code] if isinstance(code, str) else []
    if isinstance(files, dict):
        for _name, body in sorted(files.items(), key=lambda row: str(row[0]))[:64]:
            if isinstance(body, str):
                blobs.append(body)
    remaining = 65_536
    for blob in blobs:
        if remaining <= 0:
            break
        sample = blob[:min(8_192, remaining)]
        remaining -= len(sample)
        for line in sample.splitlines()[:80]:
            text = line.strip()
            if not text.startswith(DEVELOPER_FOOTPRINT_MARKER):
                continue
            raw = text[len(DEVELOPER_FOOTPRINT_MARKER):].strip()
            if not raw or len(raw) > 256:
                continue
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if valid_researcher_footprint(decoded):
                return normalize_researcher_footprint(decoded)
    return fallback


CARD_STEERING_CONTEXT_FIELDS = {
    "complexity": {"siblings", "level"},
    "eval_budget": {"remaining_seconds", "total_seconds", "stance"},
    "experiment_time_budget": {"seconds"},
    "gpu_constraint": {"mode"},
    "failure_reflection": {"node_ids"},
    "watchdog_reflection": set(),
    "trust_reflection": set(),
    "fault_localization": {"file_count"},
    "feature_engineering": set(),
    "reflection_prior": set(),
    "cross_run_advisory": {"ref", "status"},
    "cross_run_tools": set(),
    "concept_authoring": {"mode"},
    "concept_slug_reuse": set(),
    "research_memo": {"ref"},
    "strategy": {"novelty_stance", "fidelity"},
    "sweep": set(),
}
_CARD_STEERING_ENUMS = {
    "level": {"minimal", "moderate", "advanced"},
    "stance": {"explore", "selective", "exploit"},
    "mode": {"single_device", "declared_footprint", "delta", "full"},
    "status": {"available", "unavailable"},
    "novelty_stance": {"explore", "balanced", "exploit"},
    "fidelity": {"cheap", "balanced", "full"},
}


def normalize_steering_context(value) -> list[dict] | None:
    """Return one bounded ref/scalar-only Card cue snapshot, or fail the whole snapshot.

    The contract lives in ``core.models`` because both live proposal writers and the durable replay
    boundary must apply the same closed vocabulary. A future prompt/body/path field is rejected until
    explicitly reviewed; silently projecting it away would make a false lossless receipt possible.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        return None
    out: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        allowed = CARD_STEERING_CONTEXT_FIELDS.get(kind) if isinstance(kind, str) else None
        if allowed is None or "kind" not in raw or not set(raw) <= ({"kind"} | allowed):
            return None
        item = {"kind": kind}
        for key in sorted(allowed):
            if key not in raw:
                continue
            current = raw[key]
            if key == "node_ids":
                if (not isinstance(current, list) or len(current) > 16
                        or any(type(node_id) is not int or not 0 <= node_id <= (1 << 31) - 1
                               for node_id in current)):
                    return None
                item[key] = list(dict.fromkeys(current))
            elif key in {"siblings", "file_count"}:
                if type(current) is not int or not 0 <= current <= 1_000_000:
                    return None
                item[key] = current
            elif key in {"remaining_seconds", "total_seconds", "seconds"}:
                if (isinstance(current, bool) or not isinstance(current, (int, float))
                        or not math.isfinite(float(current)) or not 0 <= float(current) <= 1e12):
                    return None
                item[key] = round(float(current), 3)
            elif key == "ref":
                is_digest = (
                    isinstance(current, str)
                    and current.startswith("sha256:")
                    and len(current) == 71
                    and all(ch in "0123456789abcdef" for ch in current[7:])
                )
                is_memo = (
                    isinstance(current, str)
                    and current.startswith("memo:sha256:")
                    and len(current) == 76
                    and all(ch in "0123456789abcdef" for ch in current[12:])
                )
                if not (is_memo if kind == "research_memo" else is_digest):
                    return None
                item[key] = current
            elif key in _CARD_STEERING_ENUMS:
                if current not in _CARD_STEERING_ENUMS[key]:
                    return None
                item[key] = current
            else:
                return None
        out.append(item)
    return out


IDEA_PROPOSAL_DIGEST_V1_FIELDS = (
    "operator", "params", "rationale", "eval_profile", "eval_timeout", "theme",
    "concepts", "concept_mode", "concepts_added", "concepts_removed", "space",
    "hypothesis", "card_id", "footprint",
)


def idea_proposal_digest(idea: Idea) -> str | None:
    """Versioned exact digest of one bounded normalized durable Idea, or None when it is oversized."""
    try:
        # CODEX AGENT: V1 is a frozen semantic field set. Hashing the whole model dump would make a
        # future additive Idea default invalidate every already-stamped event when an old log is replayed.
        # Start at the durable boundary as well: an absent legacy concept envelope and an explicitly
        # authored empty envelope replay differently, so model defaults must not collapse their identity.
        durable = durable_idea_payload(idea)
        payload = {
            field: durable[field]
            for field in IDEA_PROPOSAL_DIGEST_V1_FIELDS
            if field in durable
        }
    except Exception:  # noqa: BLE001 - an advisory binding must never block proposal admission
        return None
    budget = [4_096, 65_536]  # total JSON atoms, total string/key characters

    def _complete(value, depth=0):
        if depth > 8 or budget[0] <= 0:
            raise ValueError("idea identity exceeds structural budget")
        budget[0] -= 1
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            if abs(value) > (1 << 53) - 1:
                raise ValueError("idea identity integer is outside JSON-safe range")
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("idea identity contains a non-finite number")
            return 0.0 if value == 0.0 else value
        if isinstance(value, str):
            budget[1] -= len(value)
            if budget[1] < 0:
                raise ValueError("idea identity exceeds text budget")
            return value
        if isinstance(value, list):
            if len(value) > 256:
                raise ValueError("idea identity list is oversized")
            return [_complete(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if len(value) > 256 or any(not isinstance(key, str) for key in value):
                raise ValueError("idea identity mapping is oversized or malformed")
            key_chars = 0
            for key in value:
                # Reject attacker-sized keys before ordering them. Sorting up to 256 huge strings would
                # otherwise pay comparison cost even though the digest must fail its text budget anyway.
                if len(key) > 512:
                    raise ValueError("idea identity key exceeds text budget")
                key_chars += len(key)
                if key_chars > budget[1]:
                    raise ValueError("idea identity exceeds text budget")
            budget[1] -= key_chars
            out = {}
            for key in sorted(value):
                out[key] = _complete(value[key], depth + 1)
            return out
        raise ValueError("idea identity contains a non-JSON value")

    try:
        bounded = _complete(payload)
        encoded = json.dumps(
            bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(encoded) > 131_072:
        return None
    return "idea:v1:" + hashlib.sha256(encoded).hexdigest()


def idea_proposal_ref(idea: Idea) -> dict | None:
    digest = idea_proposal_digest(idea)
    return {"v": 1, "digest": digest} if digest is not None else None


CARD_ACTION_DIGEST_V1_FIELDS = (
    "operator", "params", "space", "eval_profile", "eval_timeout", "parent_id", "parent_ids",
    "parent_generations", "scored_against", "scored_against_generation",
    "scored_against_empty", "footprint",
)


def card_action_digest(card_id: str, statement: str, action: dict) -> str | None:
    """Return the exact bounded identity of one card work item.

    This is deliberately narrower than :func:`idea_proposal_digest`: a card is a queued work item,
    not the aggregate research direction represented by ``Hypothesis``.  The digest binds the stable
    card id and immutable seed statement to the concrete build action and its freshness/parent anchors.
    Concept membership is metadata with its own completeness receipt and is intentionally not a
    prerequisite for execution.
    """
    if (not isinstance(card_id, str) or not card_id or card_id != card_id.strip()
            or len(card_id) > 256 or not card_id.isprintable()
            or not isinstance(statement, str) or not statement.strip()
            or statement != statement.strip() or len(statement) > 2_048
            or not isinstance(action, dict)):
        return None

    operator = action.get("operator")
    if (not isinstance(operator, str) or not operator or operator != operator.strip()
            or len(operator) > 64 or not operator.isprintable()):
        return None

    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("card action values must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("card action values must be finite numbers")
        return 0.0 if number == 0.0 else number

    def _params(value) -> dict[str, float]:
        if value is None:
            return {}
        if (not isinstance(value, dict) or len(value) > 64
                or any(not isinstance(key, str) or not key or len(key) > 200
                       or not key.isprintable() for key in value)):
            raise ValueError("card params are malformed or oversized")
        return {key: _number(value[key]) for key in sorted(value)}

    def _space(value) -> dict[str, list[float]]:
        if value is None:
            return {}
        if (not isinstance(value, dict) or len(value) > 64
                or any(not isinstance(key, str) or not key or len(key) > 200
                       or not key.isprintable() for key in value)):
            raise ValueError("card search space is malformed or oversized")
        out: dict[str, list[float]] = {}
        for key in sorted(value):
            values = value[key]
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError("card search-space values are malformed or oversized")
            out[key] = [_number(item) for item in values]
        return out

    def _node_id(value):
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= (1 << 31) - 1:
            raise ValueError("card node anchors must be bounded integers")
        return value

    def _generation(value):
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= (1 << 31) - 1:
            raise ValueError("card lifecycle generations must be bounded integers")
        return value

    try:
        raw_parent_ids = action.get("parent_ids", [])
        if (not isinstance(raw_parent_ids, list) or len(raw_parent_ids) > 64
                or len(set(raw_parent_ids)) != len(raw_parent_ids)):
            return None
        parent_ids = [_node_id(value) for value in raw_parent_ids]
        if any(value is None for value in parent_ids):
            return None
        parent_id = _node_id(action.get("parent_id"))
        scored_against = _node_id(action.get("scored_against"))
        raw_parent_generations = action.get("parent_generations")
        if raw_parent_generations is None:
            parent_generations = None
        else:
            if (not isinstance(raw_parent_generations, dict)
                    or len(raw_parent_generations) > 64
                    or set(raw_parent_generations) != {str(parent) for parent in parent_ids}):
                return None
            parent_generations = {
                key: _generation(raw_parent_generations[key])
                for key in sorted(raw_parent_generations)
            }
            if any(value is None for value in parent_generations.values()):
                return None
        scored_against_generation = _generation(action.get("scored_against_generation"))
        scored_against_empty = action.get("scored_against_empty", False)
        if type(scored_against_empty) is not bool:
            return None
        if ((scored_against is None and scored_against_generation is not None)
                or (scored_against is not None and scored_against_empty)):
            return None
        raw_eval_timeout = action.get("eval_timeout")
        eval_timeout = None if raw_eval_timeout is None else _number(raw_eval_timeout)
        if eval_timeout is not None and eval_timeout <= 0:
            return None
        profile = action.get("eval_profile")
        if (profile is not None and (not isinstance(profile, str) or len(profile) > 256
                                     or not profile.isprintable())):
            return None
        footprint = action.get("footprint")
        if footprint is not None:
            footprint = normalize_researcher_footprint(footprint)
            if footprint is None:
                return None
        payload = {
            "v": 1,
            "card_id": card_id,
            "statement": statement,
            "action": {
                "operator": operator,
                "params": _params(action.get("params")),
                "space": _space(action.get("space")),
                "eval_profile": profile,
                "eval_timeout": eval_timeout,
                "parent_id": parent_id,
                "parent_ids": parent_ids,
                "parent_generations": parent_generations,
                "scored_against": scored_against,
                "scored_against_generation": scored_against_generation,
                "scored_against_empty": scored_against_empty,
                "footprint": footprint,
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None
    if len(encoded) > 131_072:
        return None
    return "card-action:v1:" + hashlib.sha256(encoded).hexdigest()


def card_ownership_receipt(card_id: str, statement: str, action: dict) -> dict | None:
    """Create the v1 durable ``card_added`` ownership receipt for a concrete work item."""
    digest = card_action_digest(card_id, statement, action)
    if digest is None:
        return None
    return {"v": 1, "card_id": card_id, "action_digest": digest}


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


def normalized_hypothesis_statement(statement: str) -> str:
    import re
    return re.sub(r"\s+", " ", (statement or "").strip().lower())


def hypothesis_statement_digest(statement: str) -> str:
    """Collision-resistant identity behind the short human-readable hypothesis id."""
    return hashlib.sha256(normalized_hypothesis_statement(statement).encode("utf-8")).hexdigest()


def hypothesis_id(statement: str) -> str:
    """Stable id for a hypothesis statement so the same claim (from different ideas / a human /
    a deep-research direction) links to ONE ledger entry that accumulates evidence. A normalized
    slug + short hash: readable in the log, collision-resistant across paraphrases-of-the-exact-same
    wording (paraphrase *variation* is intentionally a new hypothesis — dedup is by exact intent)."""
    import re
    norm = normalized_hypothesis_statement(statement)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:48] or "hypothesis"
    return f"{slug}-{hashlib.md5(norm.encode('utf-8')).hexdigest()[:6]}"


def run_setup_key(command) -> str:
    """Stable identity for a run-level `run_setup` command, so a resume can tell "this exact setup
    already completed" from "not yet run" (arch-review §5 P2). A short hash of the canonical argv —
    single-sourced here (core) so the fold (`run_setup_finished` handler) and the engine's skip-check
    compute it identically without a layering violation (events/engine both import core)."""
    import hashlib
    canon = "\x00".join(str(a) for a in (command or []))
    return hashlib.md5(canon.encode("utf-8")).hexdigest()[:12]


class Hypothesis(BaseModel):
    """A first-class research hypothesis (P1). Audit-only — it never affects best-selection; it makes
    the run legible ("what have we learned?") and gives the UI a board. Mostly DERIVED by the fold
    from nodes whose `idea.hypothesis` is set (evidence + verdict computed from their outcomes); an
    `hypothesis_added` event can also register one with no evidence yet (a human ask, or a
    deep-research direction) that later accrues evidence when a matching node runs."""
    id: str
    statement: str
    source: str = "researcher"          # researcher | deep_research | human | strategist
    # open (no evaluated evidence) | testing (evidence running) | supported (an experiment improved) |
    # tested (evaluated, no improvement) | abandoned (explicitly dropped)
    status: str = "open"
    rationale: str = ""
    evidence: list[int] = Field(default_factory=list)   # node ids that tested it
    created_at_node: int = 0
    best_delta: Optional[float] = None  # best improvement-over-parent among evidence (audit)
    # FOREAGENT board prioritization (search/foresight.py): 0-based rank among the OPEN hypotheses in
    # the latest `hypothesis_ranked` event — 0 = predicted highest payoff. DERIVED each fold from that
    # event; None when unranked (no predictor run, or the card isn't open). Audit/UI only — the kanban
    # sorts open cards by it; never read by best-selection.
    priority: Optional[int] = None


class CardConceptSource(BaseModel):
    """Exact owner receipt for ``Card.concept_tags``.

    A card may accumulate evidence from several nodes with different concept producers.  One scalar
    provenance label is therefore meaningful only when the displayed tags name the exact proposal/node
    they came from.  ``complete`` distinguishes an honest explicit empty membership from an absent or
    lossy one; ``materialization_receipt`` carries the folded delta/classifier corruption causes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["card_added", "card_enriched", "node"]
    node_id: Optional[int] = Field(default=None, ge=0)
    node_generation: Optional[int] = Field(default=None, ge=0)
    provenance: Optional[Literal[
        "researcher-authored", "classifier", "operator-edited", "offline-heuristic",
        "untrusted-source",
    ]] = None
    membership_present: bool = False
    complete: bool = False
    receipt_valid: bool = True
    materialization_receipt: Optional[ConceptMaterializationReceipt] = None

    @model_validator(mode="after")
    def _coherent_owner(self) -> "CardConceptSource":
        # CODEX AGENT: a node provenance label without an exact lifecycle owner is forgeable metadata,
        # not a receipt.  Proposal-only sources deliberately carry neither a node id nor trusted producer.
        if self.kind == "node":
            if self.node_id is None or self.node_generation is None:
                raise ValueError("node concept sources require node_id and node_generation")
        elif self.node_id is not None or self.node_generation is not None or self.provenance is not None:
            raise ValueError("proposal concept sources cannot claim node identity or provenance")
        if self.complete and (
                not self.membership_present or not self.receipt_valid
                or self.materialization_receipt is not None
                or (self.kind == "node" and self.provenance is None)):
            raise ValueError("complete concept sources require an exact present membership")
        return self


class CardIdentityProvenance(BaseModel):
    """Bounded proof of where a card work-item identity came from.

    ``native`` is intentionally receipt-based, never inferred from an id's spelling.  Until the
    engine's mint/link lifecycle writes ``card_added.ownership_receipt``, every hash join, unbound
    ``card_added`` row, and node-only ``Idea.card_id`` remains a non-selectable shadow projection.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["native", "legacy_hash", "synthesized_shadow"] = "synthesized_shadow"
    source: Literal[
        "card_added_receipt", "card_added_unbound", "hypothesis_shadow",
        "node_statement_hash", "node_card_id", "merge", "unknown",
    ] = "unknown"
    durable: bool = False
    receipt_valid: bool = False
    action_digest: Optional[str] = None

    @model_validator(mode="after")
    def _coherent_identity(self) -> "CardIdentityProvenance":
        native = self.kind == "native"
        valid_digest = (
            isinstance(self.action_digest, str)
            and self.action_digest.startswith("card-action:v1:")
            and len(self.action_digest) == len("card-action:v1:") + 64
            and all(char in "0123456789abcdef" for char in self.action_digest[-64:])
        )
        if native != (
                self.source == "card_added_receipt" and self.durable
                and self.receipt_valid and valid_digest):
            raise ValueError("native card identity requires one valid durable card_added receipt")
        if not native and (self.durable or self.receipt_valid or self.action_digest is not None):
            raise ValueError("shadow card identities cannot claim a durable native receipt")
        return self


CardSelectionBlocker = Literal[
    "identity_not_native", "action_owner_missing", "action_owner_ambiguous",
    "action_receipt_incomplete", "freshness_unknown", "freshness_stale",
    "work_in_flight", "work_terminal", "work_owner_unknown", "card_terminal",
    "merged_work_items",
]


class CardSelectionProvenance(BaseModel):
    """Complete, bounded inputs used to derive ``Card.selection_ready``."""

    model_config = ConfigDict(extra="forbid")

    action_source: Literal["card_added", "node", "mixed", "none"] = "none"
    action_owner_count: int = Field(default=0, ge=0, le=257)
    action_complete: bool = False
    freshness: Literal["current", "stale", "unknown"] = "unknown"
    owner_state: Literal["none", "in_flight", "terminal", "mixed", "unknown"] = "none"

    @model_validator(mode="after")
    def _coherent_selection_source(self) -> "CardSelectionProvenance":
        if (self.action_owner_count == 0) != (self.action_source == "none"):
            raise ValueError("zero action owners require action_source=none")
        if self.action_source == "mixed" and self.action_owner_count < 2:
            raise ValueError("mixed action provenance requires multiple owners")
        if self.action_complete and self.action_owner_count != 1:
            raise ValueError("only one exact action owner can be complete")
        return self


class Card(BaseModel):
    """One immutable proposal/work item in the target Card queue (docs/23).

    ``Hypothesis`` remains the many-experiment research-direction aggregate. The current Layer-1
    migration projection also materializes legacy/hash/synthesized Card shadows so old logs retain a
    useful board, but those rows are advisory and never selection-ready. Only a unique receipt-bound
    ``card_added`` can establish native work-item identity; future Layer-3 code must consume
    ``selection_ready``, never infer executability from the compatibility ``actionable`` flag.
    """
    id: str                                             # engine-minted `card-{k}` (later) or statement hash
    statement: str                                      # the DISPLAY statement (operator-editable in L6)
    # The IMMUTABLE seed statement captured at card_added — the stable statement-hash JOIN key, held
    # separate from `statement` so an operator paraphrase (L6 card_edited) overlays DISPLAY only and never
    # un-links the card's hash-joined evidence. Defaults to `statement` for derived/legacy cards.
    seed_statement: str = ""
    source: str = "researcher"          # researcher | operator | engine | foresight | novelty | freshness
    rationale: str = ""
    created_at_node: int = 0
    # Lifecycle lane (DERIVED; frozen UI-contract vocabulary, kept OPEN so Layer 5 can add
    # speculating/built-awaiting-commit without a model rework): proposed (no node yet) | building
    # (node_building in flight) | coded (pending node with code) | running (pending eval) | evaluated
    # (>=1 terminal) | gated (only trust-gated / breed-excluded evidence) | dropped (card_dropped/merged).
    status: str = "proposed"
    # Research verdict (DERIVED via the shared `_evidence_verdict` helper — byte-identical to the
    # hash-joined hypothesis): open | testing | supported | tested | abandoned.
    verdict: str = "open"
    # Layer-1c compatibility flag for board filtering only: False for dropped/gated/abandoned, True for
    # proposed/running/evaluated. It deliberately does NOT imply that one fresh executable action exists.
    actionable: bool = True
    # CODEX AGENT: `actionable` is a compatibility/advisory board flag, never proof that a card is one
    # executable work item.  Only the receipt-backed, fail-closed seam below may be consumed by the
    # future Layer-3 queue. `selection_ready` stays False for every legacy/hash/synthesized runtime card.
    identity: CardIdentityProvenance = Field(default_factory=CardIdentityProvenance)
    selection_provenance: CardSelectionProvenance = Field(default_factory=CardSelectionProvenance)
    selection_blockers: list[CardSelectionBlocker] = Field(
        default_factory=lambda: ["identity_not_native"], max_length=16)
    selection_ready: bool = False
    evidence: list[int] = Field(default_factory=list)   # node ids that tested it (== node_ids)
    best_delta: Optional[float] = None                  # best improvement-over-parent among evidence (audit)
    # Identity / lineage.
    merged_into: Optional[str] = None                   # canonical id if this card was merged away
    aliases: list[str] = Field(default_factory=list)    # ids folded INTO this canonical card
    dropped_reason: Optional[str] = None
    dropped_by: Optional[str] = None                    # operator | engine | freshness | novelty
    # Prospective parent anchor — the Layer-5 freshness gate re-derives improve/merge legality for a
    # not-yet-built card against state.best()/rank_by_metric[:2]/breedable_nodes().
    parent_id: Optional[int] = None
    parent_ids: list[int] = Field(default_factory=list)
    # Exact lifecycle attempts captured with the action. ``None`` is a legacy/missing fence; an
    # explicit empty mapping is the complete modern snapshot for a no-parent action.
    parent_generations: Optional[dict[str, int]] = None
    # Staleness fence: the best_node_id / event seq the card was scored against (Layer-5 freshness gate).
    scored_against: Optional[int] = None
    scored_against_generation: Optional[int] = Field(default=None, ge=0)
    # Distinguishes a modern action formed with no incumbent from a legacy missing score fence.
    scored_against_empty: bool = False
    # The idea block (what to run) — populated from the linked node's Idea in Layer 1a.
    operator: Optional[str] = None                      # draft | improve | merge | debug
    params: dict[str, float] = Field(default_factory=dict)
    space: dict[str, list[float]] = Field(default_factory=dict)
    eval_profile: Optional[str] = None
    eval_timeout: Optional[float] = Field(default=None, gt=0)
    concept_tags: list[str] = Field(default_factory=list)
    # CODEX AGENT: exact, additive ownership receipt for concept_tags.  Without it a merged card could
    # show the union/override from several evidence nodes beside one misleading scalar provenance tier.
    concept_source: Optional[CardConceptSource] = None
    # Board prioritization (foresight) — stamped from card_ranked in Layer 1b; audit/UI only.
    priority: Optional[int] = None
    # True only when the final operator-priority overlay owns the priority; foresight ranks stay false.
    pinned: bool = False
    foresight_rank: Optional[int] = None
    confidence: Optional[float] = None
    # --- Layer 1b enrichment (RESERVED; populated in 1b — defaults keep Layer 1a a pure hypotheses shadow).
    # Ref-shaped ONLY (docs/23 decision 23): no verbatim source/captured-output on the card.
    footprint: Optional[dict] = None                    # {gpus, gpu_mem_mib, proposed_by, finalized_by, pinned_by}
    novelty_verdict: Optional[dict] = None              # {grade, level, near_node, recommendation}
    cross_run_prior: Optional[dict] = None              # {matched_concepts, prior_run_ids/outcomes} (refs)
    research_origin: Optional[str] = None               # memo id ref
    lesson_refs: list[str] = Field(default_factory=list)
    claim_refs: list[str] = Field(default_factory=list)
    steering_context: list[dict] = Field(default_factory=list)  # compact STRUCTURED cues (no verbatim capture)
    # Compatibility scalar for existing clients. Derived only from concept_source.provenance for an exact
    # node owner; proposal-only sources keep it None, and card_enriched cannot assign it independently.
    provenance_tier: Optional[str] = None

    @field_validator("parent_generations", mode="before")
    @classmethod
    def _bounded_parent_generations(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError("parent_generations must be a bounded mapping")
        out: dict[str, int] = {}
        for key, generation in value.items():
            if (not isinstance(key, str) or len(key) > 10
                    or not key.isascii() or not key.isdecimal()
                    or key != str(int(key)) or type(generation) is not int
                    or not 0 <= generation <= (1 << 31) - 1):
                raise ValueError("parent_generations must contain canonical bounded attempts")
            out[key] = generation
        return dict(sorted(out.items()))

    @model_validator(mode="after")
    def _selection_readiness_is_fail_closed(self) -> "Card":
        if not self.selection_ready:
            return self
        provenance = self.selection_provenance
        if not (
            self.identity.kind == "native"
            and self.identity.durable
            and self.identity.receipt_valid
            and provenance.action_source == "card_added"
            and provenance.action_owner_count == 1
            and provenance.action_complete
            and provenance.freshness == "current"
            and provenance.owner_state == "none"
            and not self.selection_blockers
            and self.status == "proposed"
            and self.verdict == "open"
            and not self.evidence
            and not [
                alias for alias in self.aliases
                if not self.seed_statement or alias != hypothesis_id(self.seed_statement)
            ]
            and self.dropped_reason is None
            and self.merged_into is None
        ):
            raise ValueError("selection_ready requires one fresh, native, unowned work item")
        return self


class ResearchMemo(BaseModel):
    """Output of the Deep-Research stage (Phase 2): the model reads across ALL results so far +
    the literature/web and writes a strategic memo that steers the next batch. Audit-only — it is
    recorded as a `research_completed` event folded into `RunState.research`, NEVER into the search
    DAG, so best-selection/policies are untouched. The UI renders it as a node and surfaces the
    `summary`/`findings`/`recommended_directions` as the conclusion; `reasoning` is debug-only."""
    summary: str = ""                                   # one-paragraph conclusion (the takeaway)
    reasoning: str = ""                                 # the "think hard" narrative (debug-only)
    findings: list[str] = Field(default_factory=list)   # concrete observations across results/web
    # D8 evidence ledger: findings as CLAIMS with per-claim provenance — {statement,
    # node_ids: [int], urls: [str]}. Kosmos's failure data says cross-evidence SYNTHESIS is the
    # weakest link (57.9% accurate vs ~85% for analysis), so every synthesis claim must be
    # traceable to the experiments/sources it rests on; the Verifier (trust/verify.py) then
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
    """Pure fold of the event log ([ADR-12]). Never mutated except by `replay.fold`.

    Field regions (docs/15 §P5.3 — banners, deliberately NOT nested sub-models: readers spell
    `st.<field>` at dozens of sites and the flat shape is additive-safe):
      1. core run state (below)            — selection-relevant: nodes/best/gates/budget;
      2. live operator control             — the `<x>_requests`/`<x>s_done` counter pairs (see
         engine invariant #3: every side effect gates on a domain event);
      3. audit-only sidecars               — folded for the UI/exports; NEVER touch selection;
      4. read helpers                      — derived views, no mutation."""
    # --- core run state (selection-relevant) ---
    run_id: str = ""
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
    # T2 trust enforcement (folded from run_started; "audit" for old logs). "gate"/"block" make
    # best-selection exclude nodes flagged for a reward-hack / data-leakage signal (not critic).
    trust_gate: str = "audit"
    # Layer 3 queue owner pinned by run_started. False on old logs preserves the policy/pilot path;
    # replay never infers this selection-affecting treatment from a mutable config snapshot.
    card_driven_selection: bool = Field(False, exclude=True)
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
    # ALL nodes currently being built, keyed by node_id — the `parallel_build>1` superset of the
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
    # pivot signal) recorded at the strategist cadence when `concept_pivot` is on. The folded field
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
    # CODEX AGENT: partial/unavailable materialization is represented by a closed, ordered reason envelope.
    # An unresolved dependency (including every active descendant) materializes to [] fail-closed; bounded
    # identity loss keeps the valid subset. The receipt prevents either fallback from being presented as an
    # exact membership by ConceptFrame. Keys are current/historic node ids; current-state projections apply
    # the same tombstone/abort lifecycle filter as memberships.
    node_concept_materialization_receipts: dict[int, ConceptMaterializationReceipt] = Field(
        default_factory=dict)
    # CODEX AGENT: proposer-authored taxonomy is an untrusted claim, never classifier evidence. This
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
    # `concept_pivot` is on; audit-only. Additive/reader-defaulted: empty on old logs -> byte-identical fold.
    hypothesis_concepts: dict[str, list[str]] = Field(default_factory=dict)
    # PART IV D4 (§21.18 B1-ext): concept-graph vocabulary SIZE when each hypothesis was tagged — a
    # hypothesis tagged against a much smaller vocabulary is STALE and gets re-tagged against the grown one
    # (bounded per cadence), mirroring node_concepts_at_vocab. Additive/reader-defaulted; empty on old logs.
    hypothesis_concepts_at_vocab: dict[str, int] = Field(default_factory=dict)
    # PART IV D5 (§21.18 B3): the accumulated concept-consolidation rename map (raw_id -> canonical_id).
    # Reused by later cadences so consolidation decisions stay FIXED (stable vocabulary, no flapping / B1
    # churn). Populated only when `concept_pivot` is on; audit-only. Additive/reader-defaulted: empty on
    # old logs -> byte-identical fold.
    concept_consolidation: dict[str, str] = Field(default_factory=dict)
    # PART IV concept-edge substrate: the typed concept graph (src, rel, dst) -> {provenance, confidence},
    # keyed by "src\trel\tdst". Makes hierarchy a swappable projection (project_hierarchy). Folded
    # COMMUTATIVELY from explicit EV_CONCEPT_EDGE assertions (max-confidence-wins per triple ->
    # order-tolerant). Derived ``co_occurs`` rows are intentionally omitted and recomputed from current
    # memberships by ConceptFrame, so stale counts can decrease/disappear. Audit-only, never touches
    # selection. Additive/reader-defaulted: empty on old logs -> path projection remains available.
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
    # These are folded from appended CONTROL events (intent). The engine remains the sole writer
    # of DOMAIN events: it reads the intent here and writes the effect (e.g. node_abort -> a
    # node_failed reason="aborted"). All deterministic under replay; audit-only fields never
    # change best-selection.
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
    forks_done: int = 0                        # count of processed forks (replay-safe fulfillment)
    # `inject_node`: an operator-authored experiment hand-added to the tree (a manual idea +
    # optional parent + optional code). The engine materializes each one into a real pending node
    # that the policy then evaluates like any other — so a human can steer the search directly.
    inject_requests: list[dict] = Field(default_factory=list)
    injects_done: int = 0                      # count of processed injects (replay-safe fulfillment)
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
    # A7 Strategist (audit-only; never read by best-selection). `active_strategy` is the latest
    # applied Strategy dict; `strategy_history` is the timeline of switches for the "why this
    # strategy" panel. `pending_strategy` is an operator override (set_strategy control event) that
    # the engine applies before consulting the Strategist (human-wins parity with pause/hint).
    active_strategy: Optional[dict] = None
    strategy_history: list[dict] = Field(default_factory=list)
    pending_strategy: Optional[dict] = None
    # A1 ASHA: rung-promotion audit trail {rung, survivors} for the UI (successive-halving view).
    rungs: list[dict] = Field(default_factory=list)
    # --- audit-only sidecars (folded for the UI/exports; NEVER touch selection) ---
    # Unified self-driving agent (audit-only; never read by best-selection): timeline of the agent's
    # macro-action choices {at_node, chosen, legal, recommended, rationale} for the "why this action"
    # view. Additive — old event logs without `agent_decision` events fold to an empty list.
    agent_decisions: list[dict] = Field(default_factory=list)
    # A6 proxy/predictive scoring: per-node early-signal scores + which candidates were skipped.
    proxy_scores: dict[int, float] = Field(default_factory=dict)
    proxy_skipped: list[int] = Field(default_factory=list)
    # B5 reward-hacking detector (audit-only; never changes selection): flagged suspicious wins
    # {node_id, signals:[{signal, detail}]} for the Trust panel.
    reward_hacks: list[dict] = Field(default_factory=list)
    # FOREAGENT predict-before-execute picks {node_id, confidence, chosen, ...}, folded from
    # `foresight_selected` events. Audit-only — never touches selection — but the fold keeps them so
    # the world model can be primed with its OWN track record (did past predicted-best picks beat
    # their parent?), closing the open predict→outcome loop (signal-delivery, §1). Additive.
    foresight_selected: list[dict] = Field(default_factory=list)
    # E1 novelty/dedup gate: near-duplicate proposals that were nudged off {node_id, near_node, ...}.
    novelty_events: list[dict] = Field(default_factory=list)
    # PART IV D3 (Phase 2b): the live gate's GRADED-ALLOW decisions — proposals allowed despite a
    # concept overlap the flat gate would reject (level-4 same-direction-new-impl, level-5 re-open of a
    # wrongly-abandoned direction). Audit-only sidecar; never read by best-selection. Additive.
    novelty_grades: list[dict] = Field(default_factory=list)
    # PART IV cross-run Step 2 (§21.20): concepts the proposed idea shares with a SIMILAR earlier run,
    # surfaced (never rejected) so the trace/researcher sees "tried in run X -> metric Y". Populated only
    # under `cross_run_concepts`; audit-only sidecar, never read by best-selection. Additive.
    cross_run_priors: list[dict] = Field(default_factory=list)
    # Deep-Research stage (Phase 2, audit-only sidecar — NEVER read by best-selection). `research`
    # is the timeline of completed memos (each a ResearchMemo dump); `research_requests` are pending
    # manual `deep_research` control events and `research_served` how many have been fulfilled (the
    # replay-safe gate, mirroring inject_requests/injects_done).
    research: list[dict] = Field(default_factory=list)
    research_requests: list[dict] = Field(default_factory=list)
    research_served: int = 0
    # Hypothesis ledger (P1, audit-only — NEVER read by best-selection). Derived each fold: from every
    # node whose `idea.hypothesis` is set, plus any explicit `hypothesis_added` events (human /
    # deep-research directions). Keyed by `hypothesis_id`. The UI renders it as a board.
    hypotheses: dict[str, Hypothesis] = Field(default_factory=dict)
    # Explicitly-added hypotheses (human `add_hypothesis` control event or a deep-research direction),
    # kept separately so the derived-from-nodes pass can merge evidence into them. `abandoned` ids are
    # a human/agent override of the derived status.
    hypotheses_added: list[dict] = Field(default_factory=list)
    # P1+ agentic merge: `hypothesis_merged` events fold ALIAS hypotheses (paraphrases the exact-hash
    # ledger kept separate) into a CANONICAL id, deterministically applied in `_derive_hypotheses`.
    hypotheses_merged: list[dict] = Field(default_factory=list)
    hypotheses_abandoned: list[str] = Field(default_factory=list)
    # Human-DELETED hypotheses (hypothesis_updated status=deleted): removed from the board entirely,
    # unlike `abandoned` (which stays visible in its own column). Excluded from `hypotheses` on fold.
    hypotheses_deleted: list[str] = Field(default_factory=list)
    # FOREAGENT board prioritization (audit-only — NEVER read by best-selection). The latest
    # `hypothesis_ranked` event: {at_node, order:[ids], confidence, reason, ranked:[{id,statement}]}.
    # `_derive_hypotheses` stamps each open card's `priority` from `order`; the UI kanban sorts by it
    # and shows `reason` as the "why this order" analysis trace. None until the predictor first runs.
    hypothesis_ranking: Optional[dict] = None
    # Hypothesis-card Kanban re-architecture (docs/23, Layer 1a). The CARD ledger — DERIVED each fold by
    # `_derive_cards` (mirrors `hypotheses`), ADVISORY, never read by best-selection. Keyed by card id.
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
    # Operator-override maps — RESERVED in Layer 1a, filled by Layer 6 control events. `_derive_cards`
    # overlays them in a FIXED LAST phase so the operator always wins regardless of event arrival order
    # (docs/23 decision 27). Empty {} in Layer 1 -> the overlay is a no-op; reserving them now means Layer
    # 6 needs no `_derive_cards` rewrite. Keyed by card id.
    card_priority_pins: dict[str, int] = Field(default_factory=dict)
    card_operator_edits: dict[str, dict] = Field(default_factory=dict)
    card_resource_pins: dict[str, dict] = Field(default_factory=dict)
    # Agent-authored run report (conclusion-first; audit-only sidecar — NEVER read by best-selection).
    # The latest `report_generated` event's content, regenerated on a cadence + on manual refresh. The
    # UI renders the deterministic analysis from the node set and layers this narrative on top.
    report: Optional[dict] = None
    # M6 comparative-lesson sidecars (audit-only — NEVER read by best-selection).
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
