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
    CARD_CHILD_LIMIT,
    CARD_CONCEPT_TAG_LIMIT,
    CARD_KINDS,
    CARD_KIND_DIRECTION,
    CARD_KIND_EXPERIMENT,
    CARD_LINEAGE_MAX_DEPTH,
    Card,
    CardConceptSource as _CardConceptSource,
    CardIdentityProvenance as _CardIdentityProvenance,
    CardSelectionBlocker as _CardSelectionBlocker,
    CardSelectionProvenance as _CardSelectionProvenance,
    DEVELOPER_FOOTPRINT_MARKER as _DEVELOPER_FOOTPRINT_MARKER,
    IDEA_PROPOSAL_DIGEST_V1_FIELDS as _IDEA_PROPOSAL_DIGEST_V1_FIELDS,
    _card_action_digest as __card_action_digest,
    card_action_digest as _card_action_digest_v2,
    card_child_rollup,
    card_drift_brief,
    card_proposal_drift,
    card_lineage_brief,
    card_rollup_brief,
    card_is_direction,
    card_kind_of,
    card_ownership_receipt as _card_ownership_receipt,
    card_score_fence_state as _card_score_fence_state,
    developer_artifact_footprint as _developer_artifact_footprint,
    durable_idea_payload as _durable_idea_payload,
    effective_card_footprint as _effective_card_footprint,
    hypothesis_concept_cache_keys as _hypothesis_concept_cache_keys,
    hypothesis_id as _hypothesis_id,
    hypothesis_statement_digest,
    idea_field_carried as _idea_field_carried,
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
idea_field_carried = _idea_field_carried
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


# WHICH CHANNEL PUT A VALUE IN `extra_metrics` — the SUBJECT question one rung down from
# `runtime/metric_subject.py`, asked of the SECONDARY numbers instead of the primary one.
#
# THE OMISSION THIS CLOSES. `runtime/command_eval.py` fills `extra_metrics` from two channels and
# only one of them is guarded:
#
#   declared — `EvalSpec.metrics`, the OPERATOR's own reader specs. It refuses `kind: "adapter"`
#              with the same words `cross_check` uses ("an agent-authored gate reader defeats the
#              trust boundary"). Used ZERO times across the whole preserved corpus.
#   auto     — `runtime/sandbox.py::json_line_extras`: EVERY other numeric key on the candidate's
#              own stdout JSON line. No declaration, no reader spec, no gate. It produced ALL of
#              them: 1,642 recorded values over 10 distinct keys, across 9 runs. (This block first
#              said "12 of 12, across 3 runs" — that count sampled three `runs/*/events.jsonl` and
#              counted a probe NODE where it meant a probe VALUE. The corrected figure is measured
#              over all 238 `*.jsonl` under `runs/`, and it is stated here because the shape of the
#              population, not just its size, is what the third channel below turns on.)
#
# They include `speculation_cuda_probe_v=1.0` — a schema VERSION number — beside `device_count`,
# `alloc_bytes` and `device_ordinal`, and beside genuine measurements like `train_auc`/`cv_mean_auc`.
# All of them were shown to the operator, exported to MLflow and served to reviewers in the same
# visual place as the protected primary metric, with nothing marking the difference. The primary
# metric has a subject, a `metric_provenance`, an enforcement rung and the protected `score` stage;
# an extra metric had none of that AND came in through the unguarded door.
#
# docs/36 is the frame: what goes into the RECORD stays deterministic over AUTHENTICATED evidence.
# A number the CANDIDATE wrote cannot be made authentic after the fact, so for that population the
# fix is not to hide it but to make the record SAY which door it came through, at every consumer.
# The third channel below is the other half: some of what arrives through this door was never the
# candidate's, and for THAT population the record can say so deterministically.
EXTRA_METRIC_DECLARED = "declared"   # read by an operator-owned `EvalSpec.metrics` reader spec
EXTRA_METRIC_AUTO = "auto"           # scraped off the candidate's own stdout; undeclared, unauthenticated
# THE ENGINE'S OWN DIAGNOSTIC, riding the auto-capture channel because it has no other door.
#
# Tagging made the channel visible; it did not stop a version number from being CALLED a metric, and
# that residual was left open on purpose. Re-measured 2026-08-14 over the WHOLE preserved corpus (238
# `*.jsonl` under `runs/`, not the three logs the first count sampled): 1,642 recorded values, 10
# distinct keys, and they are TWO populations, not one.
#
#   1,636 values / 4 keys — `speculation_cuda_probe_v`, `device_count`, `alloc_bytes`,
#       `device_ordinal`, across 7 runs. A schema VERSION, a hardware inventory count, and two
#       constants of the request the probe made. None of them measures the experiment. Every one was
#       printed by `core/calibration.py`'s own source, which the ENGINE splices ahead of the Toy
#       objective; `search/speculation_quality.py::_validate_cuda_probe_artifact` then authenticates
#       them by engine-owned code prefix, exact key schema and static values.
#   6 values / 6 keys — `train_auc`, `test_auc`, `cv_mean_auc`, `cv_std_auc`, `std`,
#       `overfitting_gap`, across 2 runs. Genuine measurements an agent-authored script printed.
#
# So `auto` was a FALSE statement about 99.6 % of the corpus: "the candidate wrote it, nobody checked
# it" is exactly backwards for a number the engine wrote and the receipt gate checks. The separating
# property is not the key's NAME (a list is the heuristic `json_line_extras` already carries, and
# `alloc_bytes`/`device_count` are perfectly good measurements for a memory benchmark) and not its
# SHAPE (see `core/calibration.py::engine_declared_extra_metric_keys` for why constancy is both
# untestable on this corpus and undecidable at capture) — it is WHO AUTHORED THE PRINT STATEMENT,
# which the engine can answer for exactly the artifacts it authored itself, byte-exactly.
#
# THE HONEST LIMIT, stated here because this constant is where a reader will look for it: this
# separates the two populations only where the engine wrote the writer. Inside an agent-authored
# artifact nothing available at capture tells a diagnostic from a measurement — `{"metric": .9,
# "seed": 42, "n_train": 5000, "val_auc": .88}` offers no signal — and there `auto` stays the
# complete and correct answer.
EXTRA_METRIC_ENGINE = "engine"
# READER-SIDE ONLY, and never written: the answer for a value whose channel the log does not record.
# Every log written before this shipped is in that state, which is why the default is NOT `declared`
# — assuming the guarded channel for an untagged value would state exactly the thing that was never
# true: every one of the preserved historical values came from `auto`. "unknown" is the honest
# reading, and every consumer must treat it as at-least-as-untrusted as `auto` (it very probably IS).
EXTRA_METRIC_UNKNOWN = "unknown"
# The channels a WRITER may record. `EXTRA_METRIC_UNKNOWN` is deliberately outside it.
EXTRA_METRIC_CHANNELS = (EXTRA_METRIC_DECLARED, EXTRA_METRIC_AUTO, EXTRA_METRIC_ENGINE)
# Channels whose value is text the CANDIDATE authored, i.e. not authenticated evidence. `unknown` is
# in here on purpose: a reader that cannot tell must not present the value as measured.
EXTRA_METRIC_UNAUTHENTICATED = (EXTRA_METRIC_AUTO, EXTRA_METRIC_UNKNOWN)
# ...and its complement, the two channels whose value something OTHER than the candidate vouched for:
# an operator-owned reader spec, or engine-owned source verified byte-exactly. Written as its own
# tuple rather than as `not in UNAUTHENTICATED` so a channel added later must be classified on
# purpose in both directions instead of defaulting into the trusted half.
EXTRA_METRIC_AUTHENTICATED = (EXTRA_METRIC_DECLARED, EXTRA_METRIC_ENGINE)
# THE READER THAT MAKES THE SENTENCE ABOVE TRUE. "Classified on purpose in both directions" is a
# claim about a tuple that, until this line, had ZERO readers in `looplab/`, `tests/` or `ui/` —
# `EXTRA_METRIC_AUTHENTICATED` is consulted by `nodes_extra_metrics` below and its complement by
# nothing at all, so a fifth channel would have joined `EXTRA_METRIC_CHANNELS`, defaulted out of
# BOTH tuples, and been silently treated as unauthenticated-by-omission by every reader that tests
# membership of the trusted half. That is the "defaulting into the trusted half" failure read in the
# mirror, and it is not caught by any test that does not already know the new channel's name.
#
# So the two tuples must PARTITION the vocabulary a reader can see, which is the writer vocabulary
# plus the one reader-side answer: exhaustive (nothing unclassified) and disjoint (nothing claiming
# both). A bare `assert` at import, like the five registry cross-checks in
# `serve/control_validation.py`: adding a channel without classifying it is a coding error to be
# fixed before the process starts, not a runtime condition to survive.
assert (set(EXTRA_METRIC_AUTHENTICATED) | set(EXTRA_METRIC_UNAUTHENTICATED)
        == set(EXTRA_METRIC_CHANNELS) | {EXTRA_METRIC_UNKNOWN}
        and not set(EXTRA_METRIC_AUTHENTICATED) & set(EXTRA_METRIC_UNAUTHENTICATED)), (
    "every extra-metric channel a reader can see must be classified as authenticated or not, "
    "exactly once")


def normalize_extra_metric_channels(value, *, max_items: int = 256) -> dict[str, str]:
    """Normalize the `extra_metrics` channel map to `{name: "declared"|"auto"}`.

    Same untrusted-input discipline as `normalize_extra_metrics` (this arrives from an old or
    hand-edited event log, and assignment validation is off): a non-dict, a non-string key, or a
    value outside the WRITER vocabulary is dropped rather than coerced. A dropped entry is not
    silently upgraded — it simply reads back as `EXTRA_METRIC_UNKNOWN` through
    `extra_metric_channel`, which is the safe direction.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        if len(out) >= max_items or not isinstance(key, str) or raw not in EXTRA_METRIC_CHANNELS:
            continue
        out[key[:200]] = str(raw)
    return out


# WHICH WAY IS BETTER on an extra metric — the second SUBJECT question about a value, beside the
# channel question one block up, and recorded the same way for the same reason.
#
# THE OMISSION THIS CLOSES, and it is a live hazard rather than a tidiness point.
# `ui/src/panels.jsx::paretoFront` builds the non-dominated set over the primary metric (which IS
# direction-aware, from `state.direction`) plus EVERY key of `extra_metrics`, each treated as
# cost-like — lower is better. That assumption is documented in the panel and has been harmless for
# exactly one reason: measured 2026-08-21 over every `*.jsonl` under `runs/` (131 files, 15 run
# directories), the only extra metrics any run has ever recorded are the engine's four CUDA-probe
# constants in the `specgate*` toys, and a CONSTANT dimension can neither create nor break a
# domination. Every one of the 8 evaluated nodes across the two real task families records
# `extra_metrics == {}`.
#
# So the panel is correct today and becomes wrong the moment a run records a real second objective —
# and the objectives waiting to be recorded are quality metrics. One vecsearch score stage already
# PRINTS nDCG@k, MAP@k, MRR@k, Precision@k and Recall@k at seven cutoffs, ~35 numbers, of which the
# record keeps one; every one of them is higher-is-better. Declaring them without this map would
# invert them on the single surface that ranks nodes, and the failure is silent: every number on
# screen is real and the ordering is backwards.
#
# WHY A MAP AND NOT A NAME RULE. "nDCG means max" is a heuristic over spellings, which is the
# mechanism-not-property shape (`docs/BACKLOG.md` §0.8 found it nine times in one day) — it answers
# for the keys someone thought of and silently mis-answers for `loss_at_100` or a domain metric
# nobody anticipated. The operator's own reader spec is where the answer belongs, because the
# operator is the party that knows.
#
# WHY "UNKNOWN" MUST NOT DEFAULT TO A DIRECTION. Same discipline as `EXTRA_METRIC_UNKNOWN`: every
# value already on disk was recorded without one, and picking either direction for those states
# something that was never measured. A consumer that cannot orient a dimension must DROP it from
# the ordering rather than guess — which is what keeps this change behaviour-preserving on the
# corpus as it stands.
EXTRA_METRIC_DIRECTION_UNKNOWN = "unknown"


def normalize_extra_metric_directions(value, *, max_items: int = 256) -> dict[str, str]:
    """Normalize the `extra_metrics` direction map to `{name: "min"|"max"}`.

    Same untrusted-input discipline as `normalize_extra_metric_channels`, and the same reason: this
    arrives from an old or hand-edited event log with assignment validation off. A non-dict, a
    non-string key, or a value outside `DIRECTIONS` is DROPPED rather than coerced, and a dropped
    entry reads back as `EXTRA_METRIC_DIRECTION_UNKNOWN` through `extra_metric_direction` — an
    unorientable dimension, which every consumer must decline to rank on."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        if len(out) >= max_items or not isinstance(key, str) or raw not in DIRECTIONS:
            continue
        out[key[:200]] = str(raw)
    return out


def extra_metric_direction(directions, key) -> str:
    """Which way is better on a single extra metric, for any reader.

    `EXTRA_METRIC_DIRECTION_UNKNOWN` when the map is absent (every log written before this shipped)
    AND when it is present but says nothing about this key. Both are the same fact — nobody recorded
    which way is better — and neither may be reported as a direction."""
    if isinstance(directions, dict):
        found = directions.get(key)
        if found in DIRECTIONS:
            return str(found)
    return EXTRA_METRIC_DIRECTION_UNKNOWN


def oriented_extra_metrics_only(extras, directions) -> tuple[dict, dict]:
    """The `(extras, directions)` pair keeping ONLY the values a reader can ORDER.

    The sibling of `authenticated_extra_metrics_only`, expressed over the recorded tag for the same
    reason: a ranking surface must not re-derive "which way is better" a second time and drift from
    the label. Everything else stays readable — this drops a dimension from an ORDERING, never a
    node from a record, and the two are not the same exclusion (`ui/src/panels.jsx` argues at length
    why dropping a POINT from a Pareto test publishes a front the record does not support; dropping
    an axis nobody can orient publishes a smaller front that it does)."""
    kept = {k: v for k, v in (extras or {}).items()
            if extra_metric_direction(directions, k) in DIRECTIONS}
    return kept, {k: extra_metric_direction(directions, k) for k in kept}


def extra_metric_channel(channels, key) -> str:
    """Which channel a single extra metric came through, for any reader.

    `EXTRA_METRIC_UNKNOWN` when the map is absent (a log written before the channel was recorded)
    AND when the map is present but says nothing about this key. Both are the same fact to a reader
    — nobody recorded where this number came from — and neither may be reported as `declared`."""
    if isinstance(channels, dict):
        found = channels.get(key)
        if found in EXTRA_METRIC_CHANNELS:
            return str(found)
    return EXTRA_METRIC_UNKNOWN


def authenticated_extra_metrics_only(extras, channels) -> tuple[dict, dict]:
    """The `(extras, channels)` pair keeping ONLY the values the CANDIDATE did not author.

    This is what `Settings.auto_extra_metrics = false` records, and it is deliberately expressed in
    terms of the TAG rather than re-deriving "which door did this come through" a second time: the
    gate cannot drift from the label, and an untagged value (`unknown`) is dropped with the auto
    ones because a reader that cannot prove where a value came from must not admit it here either.

    IT KEEPS `engine` AS WELL AS `declared`, and that is the point of the third channel rather than
    an incidental widening. The flag's question is "may an UNDECLARED number the candidate printed
    enter the record?" — and until the engine's own CUDA probe could be told apart from the
    candidate's stdout, the only available answer dropped the probe too, which silently broke
    `search/speculation_quality.py::_validate_cuda_probe_artifact` (an exact-key-schema check) and
    with it every calibration receipt. That is why the gate had to stay effectively unusable
    alongside calibration. Both kept channels are vouched for by something other than the candidate,
    which is the property the flag was always reaching for.

    Named for the property and not for one of its members: a gate called `declared_..._only` that
    keeps `engine` is the same kind of lie about the record this whole family exists to remove."""
    kept = {k: v for k, v in (extras or {}).items()
            if extra_metric_channel(channels, k) in EXTRA_METRIC_AUTHENTICATED}
    return kept, {k: extra_metric_channel(channels, k) for k in kept}


def apply_engine_extra_metric_channels(channels, code, *, engine_authored: bool):
    """Upgrade the keys this artifact's ENGINE-authored source printed from `auto` to `engine`.

    THE ONE APPLIER, so the third channel has exactly one spelling. Capture tags everything `auto`
    (`runtime/sandbox.py::stdout_extra_metric_channels` explains why `runtime` cannot answer the
    authorship question at all — it is handed an opaque string and runs it), and the engine, the only
    party that knows which artifacts it wrote itself, raises the probe's own keys afterwards.
    `engine/eval_dispatch.py` is its single call site, over `engine/speculation_gate.py::
    engine_authored_artifacts`; `engine_authored` is NOT derivable from `code`, which is the whole
    correction — see `core/calibration.py::engine_declared_extra_metric_keys` for what the byte
    prefix admitted when it was the entire grant.

    It only ever UPGRADES a key it is already given, and only from `auto`: a `declared` value is
    operator-owned and outranks this, and a key absent from the map is one the capture never saw, so
    this can neither invent a value nor overwrite a stronger claim. Total and fail-safe — an unusable
    map, a non-engine artifact or `engine_authored=False` all return the input unchanged.

    HERE AND NOT IN `core/calibration.py` for one concrete reason: that module imports NOTHING, which
    `tests/test_package_contracts.py` asserts so `core` cannot inherit whatever the probe grows. This
    function needs the channel vocabulary, which lives in this file beside its sibling gate. The
    classifier is reached through the calibration MODULE at call time, so the documented monkeypatch
    seam still observes every call."""
    from looplab.core.calibration import engine_declared_extra_metric_keys
    if not isinstance(channels, dict) or not channels:
        return channels if isinstance(channels, dict) else None
    keys = engine_declared_extra_metric_keys(code, engine_authored=engine_authored)
    if not keys:
        return channels
    return {k: (EXTRA_METRIC_ENGINE if (k in keys and v == EXTRA_METRIC_AUTO) else v)
            for k, v in channels.items()}


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
    # …and only a QUIESCENT pass of that producer (backlog F1i). The classifier may now run while an
    # evaluation is in flight, which is a strictly wider producer than the one every evidence consumer
    # here was reviewed against: it can tag a node whose result does not exist yet, and it makes tags
    # APPEAR EARLIER than they used to, which is enough on its own to move a graded-novelty admission.
    # The row stays a first-class read-model tag (that is what the in-flight pass is FOR); it simply is
    # not evidence until a quiescent pass re-states it. Absent == 0 == quiescent, so every pre-F1i log
    # answers byte-identically.
    if int((getattr(state, "node_concepts_at_pending", None) or {}).get(node_id, 0) or 0) > 0:
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


# DURABLE-PAYLOAD hygiene for `Idea.open_questions`, not a policy about the board. The board cap is
# `engine/research_cadence.py::admit_research_beliefs`, which is derived from the prompt window every
# reader can actually show; re-stating that number here is exactly how two caps come to disagree, so
# these are deliberately LOOSER and answer only "how much may one proposal write into node_created".
_REGISTERED_QUESTION_LIMIT = 8
_REGISTERED_QUESTION_CHARS = 500


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
    # The research DIRECTION this experiment serves — the card-level edge `Card.parent_card_id`
    # publishes (see `core/cards.py` for what the relation is and why it is not `belief_id`). The
    # Researcher AUTHORS it: a direction is a board row that owns no action, so it can never be
    # CLAIMED the way `card_id` above is claimed, and the only way an experiment gets filed under
    # one is by naming it. Advisory and nullable exactly like `card_id`: the fold refuses a self
    # edge, an unknown target and a cycle, so a wrong value costs a missing link and never a
    # malformed board. Rides `durable_idea_payload` -> `card_added` -> `Card.parent_card_id` and
    # `node_created` -> `Idea(**d["idea"])` for free; None => the card is a root, today's behaviour.
    # NOT part of any digest — `IDEA_PROPOSAL_DIGEST_V1_FIELDS` and the card action digests are
    # fixed tuples that do not name it, so two proposals differing only in the direction they serve
    # are still the same executable action, which is what makes filing one free.
    # The DESCRIPTION is the contract surface, not decoration: `IdeaEmission` inherits this field
    # and `agents/agent.py` hands `model_json_schema()` to the model as the emit tool's parameters,
    # so this sentence is what the Researcher actually reads about the edge.
    parent_card_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional. The DIRECTION_ID of the open research direction this experiment is meant to "
            "answer, exactly as shown in the OPEN RESEARCH DIRECTIONS block. A direction is a broad "
            "question that owns no runnable action; filing an experiment under one is how a family "
            "of minimal-change experiments is tracked to a shared verdict. Never put a DIRECTION_ID "
            "in card_id, and never name this experiment's own card here."),
    )
    # QUESTIONS THIS PROPOSAL IS NOT PURSUING — the Researcher's own channel for "I noticed something
    # worth investigating and it is not what I am proposing now". Until this shipped only deep
    # research and the operator could put a question on the board: the Researcher could ANSWER a
    # direction (`parent_card_id`) and, through `read_questions`, READ the board, and had no way to
    # ASK. A noticed-but-unpursued question left in `rationale` prose is read by nothing.
    #
    # AN OUTPUT FIELD AND DELIBERATELY NOT A TOOL. Engine invariant #1: the engine is the sole writer
    # of domain events, so a role returns a value and the ENGINE appends. A `register_question` tool
    # would append `EV_HYPOTHESIS_ADDED` from inside a tool call on the role's own thread, and that
    # event's membership in `BACKGROUND_APPENDABLE` does not license it — that membership exists for
    # the concurrent RESEARCH TASK, whose safety argument is "appending FEWER rows moves no reader's
    # position", not "any thread may append".
    #
    # Advisory, nullable and additive with reader-side defaults (invariant #5), exactly like
    # `card_id`/`parent_card_id`: it rides `durable_idea_payload` -> `node_created` ->
    # `Idea(**d["idea"])` for free, and an old log that carries neither key folds identically. NOT
    # part of any digest — `IDEA_PROPOSAL_DIGEST_V1_FIELDS` and the card action digests are fixed
    # tuples that do not name it, so two proposals differing only in the questions they file are the
    # same executable action. That is what makes asking FREE, which is the whole point: a Researcher
    # that had to spend its proposal to record a question would record none.
    #
    # OPEN[researcher-questions-not-appended] the CARRIER ships here and no engine path reads it yet,
    # so a registered question would ride `node_created` and become no board row.
    # proof:absent:idea_registered_questions@looplab/engine/research_cadence.py
    #
    # RE-MEASURED 2026-08-30 AND THE 2026-08-29 PREMISE WAS WRONG. That note said "the Researcher is
    # never ASKED for one", inferred from `open_questions` occurring ZERO times in `agents/roles.py`,
    # `agents/unified_agent.py` and `search/panel.py`. Grepping those files is the wrong instrument:
    # the ask travels through the MODEL, not through a literal. `IdeaEmission` derives from `Idea`
    # and the producer emits `IdeaEmission.model_json_schema()`, so this field's own description has
    # reached the model on every proposal since it landed — verified by dumping the schema.
    #
    # SO THE PRESCRIBED FIRST STEP ("ask in the emit schema, look at what comes back") WAS ALREADY
    # DONE, AND THE ANSWER IS ZERO: over every `node_created` row on this box — 155, not the 12 that
    # note counted — **0 carry a filled `open_questions`**. The carrier itself is sound end to end
    # (`IdeaEmission.to_idea` -> `durable_idea_payload` -> `Idea(**payload)` all preserve it, driven
    # in `tests/test_open_questions_ask.py`), so nothing is being dropped; the Researcher simply
    # never volunteers one.
    #
    # WHAT CHANGED, and it is the only untested lever: the user turn now ASKS IN PROSE. This repo has
    # already measured that prose outranks a schema-level cue, and that turn enumerated
    # params/rationale/space/hypothesis and never questions. The append half stays open and stays
    # gated on what comes back — a fresh run under this prompt is the measurement, and if it is zero
    # again the honest close is `DECLINED` with that number, not a second question channel.
    # `Idea.open_questions` still has no consumer outside this model and the memo path's own
    # same-named field.
    #
    # THE PRIOR QUESTION IS THEREFORE WHETHER IT SHOULD BE WIRED AT ALL, not how. The deep-research
    # channel already delivers questions end to end and was seen doing it on v10: 4 `open_questions`
    # -> 4 `hypothesis_added` -> 4 `direction` cards, and 2 of them gained `experiment` children whose
    # `parent_card_id` survived the fold. A second question channel earns its keep only if a
    # Researcher mid-PROPOSAL has questions the deep-research pass does not, and nobody has measured
    # that. Ask for it in the emit schema first, look at what comes back, and only then build the
    # append — the reverse order ships another field nothing fills.
    #
    # WHY IT IS STAGED rather than inlined: `EV_HYPOTHESIS_ADDED` is FOLDED, so appending it from the
    # main task inside a reservation's window moves `speculation._proposal_authority_seq`'s max-seq
    # CAS and discards a proposal the run has already PAID for — the exact hazard invariant #1
    # records for `train_monitor_alert`. The append must land outside that window, reuse
    # `research_cadence.admit_research_beliefs` (so the two writers agree about a full board) and
    # `question_concept_rows` (so both spell the positional join once). Shipping the carrier alone is
    # the "stamped and nothing consumes it" shape this repo has paid for before, which is exactly why
    # it wears a marker instead of a promise.
    #
    # The proof is over the FIX'S OWN SYMBOL (CLAUDE.md tier 1) and NOT over the string
    # `open_questions`: that literal already occurs in `research_cadence.py`'s memo path and
    # docstrings, so an `absent:` predicate on it is false the day it is written — the guard caught
    # exactly that, along with the slug being declared in three files instead of one.
    # `idea_registered_questions` exists nowhere yet; the commit that adds it turns this proof red,
    # and a red guard here means the item SHIPPED, so delete the marker. The predicate is ONE
    # whitespace-free token by construction — the guard splits on space, so `absent:def foo@path`
    # parses as the predicate `absent:def`, which it rejected.
    open_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Optional. Research questions you noticed but are NOT pursuing in this experiment — "
            "each a broad question worth its own investigation later, not a restatement of what you "
            "are proposing now. They become open research directions on the board that you or a "
            "later experiment can file work under; nothing is run because you listed it here."),
    )
    # WHAT EACH QUESTION IS ABOUT, positionally aligned with `open_questions` — the same shape and
    # the same join as `ResearchMemo.question_concepts`, resolved by the one shared
    # `engine/research_cadence.py::question_concept_rows`. A question's concept SET is its position
    # in the question lattice, so a question with no concepts is registered and lands ungrouped
    # rather than being refused.
    question_concepts: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Optional. Concept ids for each entry of open_questions, aligned by POSITION: "
            "question_concepts[i] describes open_questions[i]. Same axis/slug vocabulary as "
            "`concepts`. Omit rather than guess — a question with no concepts is still registered."),
    )
    # Hypothesis-card Kanban (docs/23, Layer 1b): the Researcher-PROPOSED resource footprint for this
    # experiment — {gpus, gpu_mem_mib, ...}. Audit-only in Layer 1 (surfaced on the card as proposed_by=
    # 'researcher'); the Developer FINALIZES it and the bin-packing scheduler CONSUMES it only in Layer 4.
    # Additive + nullable, rides durable_idea_payload -> node_created -> Idea(**d["idea"]) for free (like
    # eval_profile); None => today's behavior. Timeout is NOT here — it stays the single canonical
    # eval_timeout, clamped to a Settings ceiling (docs/23 owner decision 3).
    footprint: Optional[dict] = None

    @field_validator("card_id", "parent_card_id", mode="before")
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

    @field_validator("question_concepts", mode="before")
    @classmethod
    def _read_question_concept_rows(cls, value):
        """The ROW SHAPE has to be healed HERE, and driving it is what proved that.

        A `mode="before"` validator that merely checks the OUTER type is not enough: pydantic then
        validates each element against `list[str]` and raises on a flat `["loss/contrastive", ...]`
        BEFORE any `mode="after"` hook runs. That is exactly the 7d406cc2 defect — the only
        `list[list[str]]` in a schema, a model returning the natural flat shape, and the whole
        payload lost — reproduced in the field added to prevent it, and it survived until the shape
        was actually fed through the model rather than reasoned about.

        A flat row becomes an EMPTY row rather than being dropped: POSITION IS THE JOIN, so removing
        it would shift every later question onto its neighbour's concepts (c438f1c9, one layer down).

        THE ELEMENTS INSIDE A ROW NEED THE SAME TREATMENT, and healing only the row shape left the
        defect half-fixed: pydantic validates each row against `list[str]` and raises on
        `[["distill/teacher", 2]]` — a well-formed row with one non-string id — before
        `_bounded_question_concepts` (mode="after") can coerce anything. So the whole proposal was
        still lost over an advisory decoration, which is the outcome the paragraph above says must
        not happen. The dead giveaway that element healing was always intended and unreachable is
        that the after-validator does `str(item or "")` on values pydantic has already guaranteed
        are `str`.

        A non-string ID is DROPPED rather than blanked, and that is the opposite of the row rule
        because the join is different: a row's position joins it to a question, while the ids WITHIN
        a row are an unordered set. Coercing `2` to `"2"` would register a concept named "2" on the
        concept graph, which is worse than not registering one.
        """
        if not isinstance(value, list):
            return []
        return [[item for item in row if isinstance(item, str)] if isinstance(row, list) else []
                for row in value[:_REGISTERED_QUESTION_LIMIT]]

    @field_validator("open_questions", mode="before")
    @classmethod
    def _read_registered_questions(cls, value):
        """HEAL, never raise — and `IdeaEmission` deliberately does NOT override this with a strict
        twin, which is the one design decision in this field worth arguing.

        The concept envelope beside it IS strict on the emission path, because a wrong membership
        corrupts the concept graph and the model must be asked to try again. The opposite is true
        here, and 7d406cc2 is the measurement: `_MemoOut.question_concepts` was the only
        `list[list[str]]` in that schema, a model returned the natural FLAT shape, and the strict
        finalizer discarded nine good fields with it — two complete deep-research passes, 203 tool
        calls and 64 sources, thrown away over a decoration. Registering a question is strictly
        less valuable than the experiment carrying it, so a malformed value costs the QUESTION and
        never the proposal.

        Bounded here rather than at the append site because this rides a DURABLE payload: a model
        that returns two hundred questions must not write two hundred of them into `node_created`.
        The bound is deliberately loose — `engine/research_cadence.py::admit_research_beliefs` owns
        the real board cap, and duplicating that number here is how the two come to disagree.

        "HEAL, never raise" needs the ELEMENTS too, and for one day it healed only the outer type:
        pydantic then validated each entry against `str` and raised on `["ok", 3]`, and on the very
        ordinary `[{"question": …, "why": …}]` a model returns when asked for research questions —
        so the whole proposal was lost exactly as in 7d406cc2. A non-string becomes `""` and KEEPS
        its slot, because position is the join with `question_concepts`; the blank is then dropped
        from the board by `admit_research_beliefs`, which is the same treatment an unusable string
        already gets one validator down. Deliberately NO key-guessing on a dict: picking `question`
        or `statement` out of it would be this validator inventing content, and a question nobody
        wrote is worse on the board than one that was never registered.
        """
        if not isinstance(value, list):
            return []
        return [item if isinstance(item, str) else ""
                for item in value[:_REGISTERED_QUESTION_LIMIT]]

    @field_validator("open_questions", mode="after")
    @classmethod
    def _bounded_question_statements(cls, value):
        """Bound each statement WITHOUT changing the list's length — position is the join.

        The first cut of this dropped unusable entries, and driving it caught the consequence
        immediately: `["q1", "", "q3"]` became a 2-entry list while `question_concepts` still held
        3 rows, so "q3" joined to row 1. That is c438f1c9's defect re-created inside the validator
        written to carry its fix — the same trap, one layer up, and reasoning about it was not what
        found it.

        So an unusable entry becomes `""` and KEEPS its slot.
        `engine/research_cadence.py::question_concept_rows` is the one place blanks are skipped, and
        it skips them AFTER reading the index; `admit_research_beliefs` drops them from the board.
        Neither ever sees a shifted list.
        """
        bounded = []
        for item in value:
            text = str(item or "").strip()
            bounded.append(text[:_REGISTERED_QUESTION_CHARS] if text and text.isprintable() else "")
        return bounded

    @field_validator("question_concepts", mode="after")
    @classmethod
    def _bounded_question_concepts(cls, value):
        # The row SHAPE is already healed in `_read_question_concept_rows`; this only bounds the ids
        # inside each row. An emptied row is KEPT — `question_concept_rows` reads it as "no concepts
        # for that question", exactly as it reads a missing one, and keeping it holds the position.
        return [[text[:_REGISTERED_QUESTION_CHARS] for text in
                 (str(item or "").strip() for item in row[:64])
                 if text and text.isprintable()]
                for row in value]

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
        # BOTH card edges, and both strict for the same reason: base `Idea`'s validator HEALS a
        # malformed id to None, which is right for a durable log and wrong for a live producer —
        # the model would silently lose the link it authored and never be asked to try again.
        for field in ("card_id", "parent_card_id"):
            raw_card_id = value.get(field)
            if raw_card_id is not None:
                if (not isinstance(raw_card_id, str) or raw_card_id != raw_card_id.strip()
                        or not raw_card_id or len(raw_card_id) > 256
                        or not raw_card_id.isprintable()):
                    raise ValueError(f"{field} must be a bounded printable string")
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
# `oom` covers TWO signatures that look nothing alike and it needs both, because it shipped with only
# the first: the KERNEL kill (SIGKILL, exit -9/137, no traceback) and the ALLOCATOR raise
# (`torch.OutOfMemoryError`, a full traceback, exit 1). The second matches none of the first's
# conjuncts, so until 2026-08-20 every GPU exhaustion was classified `crash` and the Developer got
# "diagnose the root cause" instead of "fit in less memory" — measured on `runs/e5small-dr-unified-v3`,
# where all three nodes OOMed, all three recorded `reason: crash`, and the run stopped systemic having
# produced no metric. They stay ONE reason because the directive is the same one ("cut the memory")
# and neither `crash_repair.py` nor `_rule_triage` has anything different to say to them; what the
# text had to gain was the traceback, which only the second shape has.
# `diverged` and `stalled` are the two WATCHDOG verdicts, and they are separate members for the same
# reason: both are tree-kills the ENGINE issued, so both exit -9 with no traceback — byte-identical to
# the kernel OOM signature `_failure_reason` recognises. Measured cost of that conflation on
# rubertlite-dr-unified-v6 node 5: the DIVERGE watchdog stopped a training whose loss went 1.2e+25
# then NaN, the classifier called it `oom`, and the Developer spent three repair rounds halving the
# batch size (8192 -> 2048 -> 512 -> 256) at ~3 GPU-minutes each while the actual instability went
# untouched. "Reduce memory" and "stabilise the numerics" are opposite directives.
# `not_learning` is `diverged`'s twin and had to be its own word for the reason the paragraph above
# gives about `oom`: a FINITE loss that stopped descending and a NON-FINITE one need opposite
# directives ("the model is not learning what you told it to" vs "stabilise the numerics"), and the
# deterministic diverge watchdog cannot see the first at all — 8.8534 forever is a perfectly finite
# number. It is what the live judge names when the fault is the IMPLEMENTATION rather than the idea,
# and being in this tuple is exactly what buys the Developer a look at its own code instead of a
# terminal verdict against a hypothesis that was never tested. Like `needs_failed`, it needs no
# legacy row: no pre-field node could produce it, so there is no historical treatment to preserve.
# `unclassified` (2026-08-20) is the THIRTEENTH, and it is the only member no classifier produces:
# `triage._failure_reason` cannot return it and no watchdog names it. It is minted by
# `engine/evaluate.py` when the failure DIAGNOSTICIAN was wired, was asked, and did not answer
# readably — see `engine/failure_diagnosis.py::UNCLASSIFIED_REASON` for the four properties that
# make it safe to route (bounded blind repair, never in `NEVER_SALVAGED_REASONS`, no extra attempt,
# and countable through `REASON_SOURCE_UNDIAGNOSED`). It is in this tuple, and therefore in the
# default `Settings.inline_repair_reasons`, because a node must not be thrown away over a flapping
# provider; `tests/test_inline_repair_reason_coverage.py` derives the producer set from all THREE
# producers rather than from the classifier alone.
# `check_false_positive` (2026-08-21) is the FOURTEENTH, and the second member no classifier
# produces: it is answer-only, like `oom` and `not_learning`, and only the failure DIAGNOSTICIAN can
# ever emit it. It exists because `check_failed` names the stage that REFUSED and says nothing about
# why, so "the stage really did fail, here is the cause" and "the stage did not fail, the check was
# wrong" collapsed into one word. Measured on `bench-out/cand.durable.jsonl`: of 22 `check_failed`
# rows, 14 become `not_learning` and FIVE are answered back as `check_failed` — and reading those
# five's rationales, the diagnostician is refuting the checker with validation numbers from the same
# log ("the run actually reached val recall@100=0.8114 … yet the verifier flagged"). It was right
# and had nowhere to put it. Like `not_learning` and `unclassified` it is deliberately ABSENT from
# `metric_salvage.NEVER_SALVAGED_REASONS`, so it can neither suppress a metric nor admit one — a
# model saying "the check was wrong" must not thereby score the node (docs/36). What it buys is the
# record and the DIRECTIVE: `crash_repair._repair_error_context` points the repair at the check
# instead of asking a Developer to rewrite an experiment it has just been told is correct.
FAILURE_REASONS: tuple[str, ...] = ("crash", "timeout", "oom", "setup", "no_metric", "drift",
                                    "unclassified",
                                    "expect_failed", "check_failed", "diverged", "stalled",
                                    "needs_failed", "not_learning", "check_false_positive")


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
    # WHICH CHANNEL EACH EXTRA METRIC CAME THROUGH: `{name: "declared"|"auto"}` (see
    # `EXTRA_METRIC_CHANNELS`). Additive with a reader-side default (invariant #5): absent on every
    # log written before 2026-08-14 -> `{}` -> every key reads back `EXTRA_METRIC_UNKNOWN`, which is
    # what the fold and every consumer must SAY rather than quietly assuming the guarded channel.
    # A key missing from a PRESENT map reads `unknown` for the same reason — a later merge (trial
    # collapse, salvage gates) that forgot to tag must not inherit its neighbours' authority.
    extra_metrics_provenance: dict[str, str] = Field(default_factory=dict)
    # WHICH WAY IS BETTER on each extra metric: `{name: "min"|"max"}` (see
    # `normalize_extra_metric_directions`). Rides beside the values for the same reason the channel
    # map does — a ranking surface that re-derives it from the key's SPELLING answers for the names
    # someone thought of and silently mis-answers the rest. Additive with a reader-side default: `{}`
    # on every log written before this shipped, so every key reads `EXTRA_METRIC_DIRECTION_UNKNOWN`
    # and no consumer may order on it. A key missing from a PRESENT map reads `unknown` for the same
    # reason its channel does.
    extra_metrics_direction: dict[str, str] = Field(default_factory=dict)
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

    @field_validator("extra_metrics_provenance", mode="before")
    @classmethod
    def _normalize_extra_metrics_provenance(cls, value):
        return normalize_extra_metric_channels(value)

    @field_validator("extra_metrics_direction", mode="before")
    @classmethod
    def _normalize_extra_metrics_direction(cls, value):
        return normalize_extra_metric_directions(value)
    # Transient re-run marker (node_reset): "propose" | "implement" set it so the engine RE-RUNS this
    # existing node in place from that stage; cleared once the re-run's node_created lands. ("eval" resets
    # just clear the terminal — the node becomes pending-with-code and the normal eval loop re-scores it,
    # no marker needed.) Not persisted meaningfully — always None on a settled node.
    rerun_from: Optional[str] = None
    # Multi-stage eval pipeline (Phase 1): per-stage outcomes
    # [{name, status, exit_code, seconds, repairs}] in run order (from stage_finished events);
    # `failed_stage` names the stage that broke a failed node. Both empty/None on the classic
    # single-command eval.
    #
    # `repairs` is the REPAIR EPOCH the row was recorded in, and it is what makes a stage row
    # ATTRIBUTABLE. `stage_finished` is appended once per ATTEMPT of the inline-repair loop
    # (`engine/evaluate.py`) and the fold keeps it last-wins BY STAGE NAME, so after a repair the
    # surviving rows still describe the attempt the repair superseded — with nothing in them saying
    # so. Measured on `runs/rubertlite-dr-unified-v9` 2026-08-17: node 5 had trained for 177
    # minutes under repair #3 while its newest recorded stage statements were `mine expect_failed`
    # (epoch 2) and `train fail` (epoch 1), which every surface renders as a red ✗. Over the four
    # runs whose stage rows are written inside the attempt loop (v6-v9; a pre-2026-08-07 log wrote
    # them all at the terminal, after every repair, so it cannot express this) there are 44 such
    # windows, MEDIAN 66.1 minutes and 99.7 hours in total.
    #
    # DERIVED BY THE FOLD FROM LOG ORDER, not carried on the event — deliberately, because the
    # event carries no such field and never has, so a writer-side column would leave every row
    # already on disk unattributable while the ORDER that answers it is right there in the log.
    # `repairs` below is the same counter's current value; a row whose `repairs` is SMALLER
    # describes a superseded attempt (`stage_row_superseded`).
    stages: list = Field(default_factory=list)
    failed_stage: Optional[str] = None
    # Inline repairs applied to THIS lifecycle generation — the count of folded `node_repaired`
    # rows, which is `engine/evaluate.py::_durable_repair_ledger`'s `attempt` seen from the fold
    # side (a `salvage_cause_fix` row re-states the ordinal it FOLLOWS rather than opening a new
    # one, and taking the MAX keeps that row from charging an attempt here either). Reset to 0 by
    # `node_reset`, which opens a new lifecycle whose repair budget genuinely starts fresh.
    # Absent in old logs -> 0, and a log with no `node_repaired` rows folds to 0 exactly as before.
    # This is NOT `attempt`: that is the lifecycle GENERATION, bumped only by `node_reset`, and the
    # two spellings are cross-referenced here and on `attempt` below because merging them would
    # make an inline repair look like a reset to every reader of either.
    repairs: int = 0
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
    # IN-run fork provenance (docs/36): set when an OPERATOR branched this node off an existing one
    # and edited its idea — typically from a historical snapshot, where the node they were reading is
    # not the node the live tail holds. {"node_id","generation","observed_seq","base_idea_digest",
    # "changed_fields"} — see `serve/control_validation.py::_normalize_fork_receipt` for which of
    # those the operator supplies and which the server derives. Deliberately NOT `origin`, which
    # means a SIBLING RUN and is redacted from every review capability for that reason. Audit/UI
    # only; eval/confirmation/best-selection treat the node like any other injected one.
    forked_from: Optional[dict] = None
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
    # The durable promise/receipt plus its live-owner projection
    # (events/types.py::EV_NODE_EVAL_STARTED).
    # `eval_start_boundary` is stamped on every current engine-written `node_created` and says the
    # writer of THIS node promises to append a `node_eval_started` row before any sandbox work — so
    # for such a node the ABSENCE of one
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
    # Durable budget receipt: at least one sandbox admission happened in this lifecycle. It stays
    # true across an engine crash/resume so already-spent compute can never be refunded as "unused".
    eval_started: bool = Field(default=False, exclude=True)
    # Live-owner receipt: the CURRENT engine invocation admitted this lifecycle. A new owner clears
    # it while preserving ``eval_started``; re-admission appends another generation-matched
    # ``node_eval_started`` row and sets it again. This separation keeps the UI's "training now"
    # claim from reusing a historical budget fact after a process crash.
    eval_activity_started: bool = Field(default=False, exclude=True)
    # Timestamp of the first generation-matched eval-start row for the CURRENT owner. Duplicates in
    # one invocation do not refresh it; a genuine re-admission after resume does. Fold-internal like
    # the receipts above and ``None`` when no usable event timestamp was recorded.
    eval_started_at: Optional[float] = Field(default=None, exclude=True)

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


def stage_row_superseded(row, repairs) -> bool:
    """Does this stage row describe an attempt a LATER inline repair has already replaced?

    THE ONE SPELLING of the comparison, because it is the whole content of the fix and its two
    readers are in different languages: `serve/` hands the row and `Node.repairs` to the browser
    (`ui/src/stageAttribution.js::stageRowSuperseded` is the mirror) and `looplab inspect`/any
    python reader asks it here. Hoisted rather than inlined for the reason CLAUDE.md's guard-test
    ladder gives at tier 2 — a rule buried in a render expression is a rule no test can state.

    STRICTLY LESS-THAN, and each of the three ways that matters:
      * EQUAL is the current attempt and must read exactly as it does today. This is the negative
        control: a node whose last stage row IS its state (`repairs == Node.repairs`, including
        both being 0, which is every node that was never repaired and every pre-2026-08-07 log)
        answers False and nothing about its rendering moves.
      * GREATER happens after a `node_reset`, which resets `Node.repairs` to 0 while an eval-type
        reset RETAINS the stage rows strictly before its restart boundary. Those retained rows are
        the new lifecycle's own starting truth — their artifacts are what it reuses — so they are
        not superseded, and `_on_node_reset` re-stamps them to the fresh epoch so the number a
        surface prints beside them is not from a generation that no longer exists.
      * ABSENT (`None`) on either side answers False. An old projection carries no `repairs` key at
        all, and "I cannot tell" must render as the historical view rather than as a claim that a
        real result is stale — the same absent-is-not-zero rule `ui/src/traceProjection.js` states
        for omitted-span counters.

    THE ROW PREDICATE IS THE WHOLE PYTHON SURFACE, and that is deliberate. A
    `superseded_stage_rows(node)` list wrapper sat beside this function until 2026-08-19 with zero
    production callers — its only asker was the test restating it — because the surface that renders
    a stage strip is the BROWSER, and `serve/routers/reviews.py` deliberately hands the client
    `stages` PLUS `repairs` (see its `_REVIEW_NODE_KEYS` note) rather than a list somebody already
    derived. A python caller that wants the set writes the one-line comprehension at its own call
    site; what may never be written twice is the COMPARISON below, and that is what lives here.
    """
    if not isinstance(row, dict):
        return False
    row_repairs = row.get("repairs")
    # `bool` is an `int` in python and would compare as 0/1; a hand-edited or foreign row carrying
    # `repairs: true` is a corrupt stamp, not epoch 1, and must not convict a real result.
    if isinstance(row_repairs, bool) or isinstance(repairs, bool):
        return False
    if not isinstance(row_repairs, int) or not isinstance(repairs, int):
        return False
    return row_repairs < repairs


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
    # THE SPLIT, and it exists because the FIELD NAME contradicted its own description. The prompt
    # asked for `recommended_directions` and defined them as "(specific next experiments to try)",
    # so the model correctly returned EXPERIMENTS and the channel called them directions. Measured on
    # `runs/e5small-dr-unified-v5`: of five, exactly ONE was a genuine direction (a family —
    # "cross-encoder / strong-teacher distillation"); #2 was a single-knob experiment with an exact
    # value ("test temperature 0.01"), #1 and #3 were two concrete actions each. A one-knob
    # experiment arriving through a channel that carries no action is unbuildable forever, so the
    # memo already decided an experiment the engine then could not run.
    #
    # Both are ADDITIVE and `recommended_directions` keeps its meaning byte-for-byte: every log on
    # disk carries only it, every reader keeps working, and a memo that fills neither new field
    # folds exactly as it always did. The registration path prefers `open_questions` when present —
    # those are the rows that legitimately own no action — and leaves `next_experiments` to be
    # proposed as real work.
    open_questions: list[str] = Field(default_factory=list)    # families, no action, may not be built
    # WHAT EACH OPEN QUESTION IS ABOUT, positionally aligned with `open_questions`.
    #
    # IT WAS MISSING AND `_assemble` ASSIGNED IT ANYWAY, which is a sharper defect than a dropped
    # value: `agents/deep_research.py::_assemble` sets the memo's fields one by one and its line
    # `memo.question_concepts = clean["question_concepts"]` raised on a model with no such field —
    # so `memo.sources` on the NEXT line was never set and the call died half way. Because
    # `_assemble` mutates the memo IN PLACE, everything assigned before that line survived in the
    # object, and a memo could come back looking populated while being the product of a crashed
    # assembly. Every deep-research memo on this box went through that path.
    #
    # The durable event never showed it because `core/advisory_payloads.py::sanitize_research_memo_payload`
    # builds its own dict and defaults this key to `[]` when the source lacks it — so the row said
    # "no concepts" about a memo whose carrier could not hold any. Two writers, one payload; the
    # sanitizer was looking for a key the object was structurally unable to provide.
    question_concepts: list[list[str]] = Field(default_factory=list)

    next_experiments: list[str] = Field(default_factory=list)  # one concrete change each
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
    # F1d: the RUN-LEVEL DECLARED ENVIRONMENT this run's evals actually ran under, pinned by
    # `run_started` (`{}` on old logs and on every run that declared none — the writer omits the key
    # in that case, so the default payload stays byte-identical). It is folded because it SHAPES
    # RESULTS: `VS_LOCAL_DATA_ROOT` decides which corpus a node trained on, and a resume that read a
    # different value from live config would silently produce results incomparable with the ones
    # already in the log. `Engine._repin_declared_env` is the consumer (invariant #6). Excluded from
    # the public dump for the same reason `card_driven_selection` is: it is re-entry authority, not
    # search state, and dumping it would put the operator's declaration into every state payload the
    # UI serves.
    eval_env: dict = Field(default_factory=dict, exclude=True)
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
    # docs/29 F1 — the widths this run RE-PINNED mid-run from what the research proposed
    # (`events/replay.py::_on_run_width_settled`, written by
    # `engine/orchestrator.py::_settle_proposal_width`). SEPARATE fields from the two pins above, for
    # exactly the reason `speculation_depth_settled` is separate from `speculation_depth_pinned`: each
    # fact has ONE writer, neither handler reads the other's field before writing its own, so the two
    # rows may be spliced in either order and land on the same treatment (invariant #5). Folding a
    # repin ONTO the pin would not be order-tolerant — `_on_run_started` ASSIGNS, so a repin row folded
    # ahead of it would simply be overwritten, which is the measured defect the depth pair records.
    #
    # `None` = this run never repinned = the `run_started` pin stands, which is every pre-existing log
    # and every run whose proposals never moved the width. The resolution of the two lives in
    # `Engine._repin_settled_widths` (the re-entry hook) rather than in the fold, because the third
    # layer of the precedence — an operator's `budget_extend` — is applied by the engine per turn and
    # has to win over both. Excluded from the public dump like the pins they qualify.
    eval_parallel_settled: Optional[int] = Field(default=None, ge=1, le=1024, exclude=True)
    llm_parallel_settled: Optional[int] = Field(default=None, ge=1, le=64, exclude=True)
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
    # The number of nodes still PENDING when the classifier produced this node's CURRENT tags (backlog
    # F1i). `> 0` means the pass ran BESIDE a live evaluation, which is a producer this repo has never
    # reviewed as EVIDENCE: the node's own result/log excerpts may not exist yet, the vocabulary is
    # whatever the run had reached mid-flight, and — the load-bearing half — a tag that appeared at a
    # different INSTANT than it would have before can flip a graded-novelty admission. So the flag is
    # the EVIDENCE gate for the in-flight cadence, read by `classifier_verified_node_concepts` and by
    # `engine/novelty.py::_graded_novelty_precheck`; the read models are deliberately untouched, which
    # is the entire point of tagging mid-eval (the operator sees the tag; selection does not).
    # Additive/reader-defaulted: absent == 0 == quiescent, which is EXACTLY right for every log written
    # before F1i, because `_should_consult_concepts` could not fire with a pending node at all.
    node_concepts_at_pending: dict[int, int] = Field(default_factory=dict)
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
    # The reopen receipts, resolved against `cards_dropped` by `_event_index` (last one wins). Kept
    # as its OWN list rather than by removing the drop: the drop carries who/why, which a reopened
    # card's history still owes the operator.
    cards_reopened: list[dict] = Field(default_factory=list)
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
    # THE CROSS-NODE REPAIR LEDGER — what OTHER nodes had to fix, and why.
    #
    # Lineage carries FILES from parent to child, and it carries them correctly (see
    # `repo_developer.implement_from`). What it cannot carry is a fix discovered by a SIBLING,
    # because a node only becomes a parent by WINNING ON METRIC. Measured on
    # `runs/e5small-dr-unified-v4`: nodes 4,5,6,8,9 all improve from node 3 — a star, not a chain.
    # Node 6 hit `stage 'mine' exited 0 without producing its artifact`, its repair wrote a `prep.py`
    # that fixes it, and node 6 scored 0.781781 against node 3's 0.790898. So node 6 will never be a
    # parent, its `prep.py` is unreachable, and node 8 inherited node 3's six files verbatim and hit
    # the identical failure. Three nodes paid for the same discovery.
    #
    # The mechanism is right and is NOT changed: a mechanical fix and a scientific result are being
    # selected by the SAME criterion, and only one of them should be. So this is a second channel
    # carrying INFORMATION, never files — a later Developer is told what was repaired and why, and
    # decides for itself. It cannot corrupt a lineage because nothing is inherited through it.
    #
    # Bounded on purpose (`_REPAIR_LEDGER_MAX`): a long run repairs many times, and a ledger that
    # grows without limit becomes a prompt that crowds out the code it is meant to annotate.
    repair_ledger: list[dict] = Field(default_factory=list)

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

    def open_research_beliefs(self, *, only=None) -> list["Card"]:
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
        hand-assembled `RunState` that never went through `fold`.

        `only` FILTERS BEFORE THE COLLAPSE, and that order is the whole reason it is a parameter
        rather than the caller's own list comprehension. The representative is the FIRST no-evidence
        card of its belief, and `open_research_cards()` includes ACTION-OWNING work items (this
        docstring says so two paragraphs up), so a caller wanting only pure beliefs and filtering
        AFTERWARDS does not narrow the group — it DELETES it: the work item is elected, the filter
        drops it, and the pure sibling that shares its wording is never reached, because the collapse
        already discarded it. `engine/research_cadence.py::_admissible_beliefs` filtered afterwards
        and paid exactly that: the belief vanished from its dedup universe, a later memo restating
        the question registered a SECOND card for work already under way, and the open population
        then grew unbounded — the outcome that method's own two-population split exists to prevent,
        arriving through a different door. A predicate rather than an import because `core` may not
        import `engine`, and `None` is byte-for-byte the historical behaviour."""
        seen: set[str] = set()
        out: list["Card"] = []
        for c in self.open_research_cards():
            if c.evidence:                  # untested only (mirrors the consumers' `if not c.evidence`)
                continue
            if only is not None and not only(c):
                continue
            key = c.belief_id or hypothesis_statement_digest(c.seed_statement)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out

    def repair_candidates(self) -> list[dict]:
        """The repair ledger grouped by FILE PATH, most-repeated first — the operator's list of
        changes that belong in the source repo rather than in one node.

        A node inherits its parent's files, and correctly; what it cannot inherit is a fix a SIBLING
        found, because a node becomes a parent only by winning on metric. Measured on
        `runs/e5small-dr-unified-v4`: nodes 4, 5, 6, 8 and 9 all improve from node 3, so node 6's
        repaired `prep.py` — which diagnosed the mine-stage artifact contract correctly — was
        unreachable to node 8, and node 8 hit the same failure with node 3's six files verbatim.

        Grouped by path and counted by DISTINCT NODES on purpose: one node repairing the same file
        four times is one discovery, and four nodes repairing it once each is a property of the
        repo. The second is what deserves a commit; the first is a node having a bad day.

        This RANKS, it decides nothing. Promoting a fix into the source repo moves the substrate
        every later node is measured on — that is an operator's call, and it has to be recorded as
        an event or the comparability key cannot tell nodes on either side of it apart."""
        by_path: dict[str, dict] = {}
        for row in self.repair_ledger:
            node_id = row.get("node_id")
            for path in (row.get("paths") or []):
                if not isinstance(path, str):
                    continue
                entry = by_path.setdefault(path, {"path": path, "nodes": set(), "reasons": {}})
                # A row with no `node_id` (a hand-edited or pre-stamping log) is attributable to no
                # DISTINCT node, so it may not swell the count: `None` in this set made `node_count`
                # one higher than the `nodes` list it is rendered beside, and `node_count >= 2` is
                # the exact conjunct that fires this command's headline "N separate experiments
                # fixed <path>" recommendation.
                if node_id is not None:
                    entry["nodes"].add(node_id)
                reason = row.get("reason")
                if isinstance(reason, str):
                    entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
        out = [{"path": e["path"],
                "nodes": sorted(e["nodes"]),
                "node_count": len(e["nodes"]),
                "reasons": dict(sorted(e["reasons"].items(), key=lambda kv: (-kv[1], kv[0])))}
               for e in by_path.values()]
        out.sort(key=lambda e: (-e["node_count"], e["path"]))
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
    ever dispatched: it costs one Developer BUILD and touches no sandbox/GPU.  Charging it a
    node-budget slot is budget THEFT from the experiments the run still has to execute, so the slot
    is refunded.

    THAT SENTENCE READ "exactly one Developer call" UNTIL 2026-08-28, AND A BUILD IS NOT ONE CALL.
    Measured on `e5small-dr-unified-v9`: card-3's first build was 274 generations and 12.7M tokens,
    its rebuild 458 and 22.6M; cards 3 and 4 together — every node either ever owned discarded here —
    cost 68.9M, 21.0 % of the run at 17.6 h.  The refund argument is unaffected, because it is about
    the NODE BUDGET (GPU slots) and nothing else, and none of those tokens bought sandbox time.  But
    the old wording made the discard read as free, and it is the most expensive thing the engine does
    that produces no experiment.  `looplab tokens` now prices it per card (`events/token_spend.py::
    token_spend_by_card`) so the trade can be weighed rather than assumed.  A speculative node that DID consume an evaluation is a real experiment and keeps
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
    (``events/card_ledger.py::_card_debug_leaf_children``) has to answer that same question — a child the
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
