"""Bounded public wire projection for the derived hypothesis-card board."""
from __future__ import annotations

import heapq
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from looplab.core.concepts import normalized_concept_materialization_receipt


# CODEX AGENT: these are replay inputs/override journals, not a public read model. Excluding them before
# Pydantic serialization prevents future producer-only fields and oversized event blobs from being copied
# into every owner state/SSE frame and review summary.
INTERNAL_CARD_STATE_FIELDS = frozenset({
    "cards_added", "cards_merged", "cards_dropped", "cards_enriched", "card_ranking",
    "card_priority_pins", "card_operator_edits", "card_resource_pins",
})

PUBLIC_CARD_MAX_COUNT = 256
PUBLIC_CARD_MAX_BYTES = 8_192
PUBLIC_CARDS_MAX_BYTES = 512 * 1_024
PUBLIC_CARDS_PROJECTION_MAX_BYTES = 512 * 1_024
_MAX_ITEMS = 32
_MAX_TEXT_BYTES = 2_048
_MAX_REF_BYTES = 256
_SKIP = object()

# CODEX AGENT: this is the explicit wire DTO. Adding a Card or event field does not publish it until this
# boundary is reviewed; fixed order also makes byte output deterministic across event/mapping order.
_FIELDS = (
    "id", "status", "verdict", "actionable", "concept_source", "statement", "seed_statement", "source",
    "created_at_node", "rationale", "evidence", "best_delta", "merged_into", "aliases",
    "dropped_reason", "dropped_by", "parent_id", "parent_ids", "scored_against", "operator",
    "params", "space", "eval_profile", "concept_tags", "priority",
    "foresight_rank", "confidence",
    "footprint", "novelty_verdict", "cross_run_prior", "research_origin", "lesson_refs",
    "claim_refs", "steering_context", "provenance_tier",
)
_TEXT_LIMITS = {
    "statement": _MAX_TEXT_BYTES,
    "seed_statement": _MAX_TEXT_BYTES,
    "rationale": 800,
    "dropped_reason": 800,
}
_REF_FIELDS = {
    "id", "source", "status", "verdict", "merged_into", "dropped_by", "operator",
    "eval_profile", "research_origin", "provenance_tier",
}
_INT_FIELDS = {"created_at_node", "parent_id", "scored_against", "priority", "foresight_rank"}
_FLOAT_FIELDS = {"best_delta", "confidence"}
_REF_LIST_FIELDS = {"aliases", "concept_tags", "lesson_refs", "claim_refs"}
_INT_LIST_FIELDS = {"evidence", "parent_ids"}
_FOOTPRINT_KEYS = {"gpus", "gpu_mem_mib", "proposed_by", "finalized_by", "pinned_by"}
_NOVELTY_KEYS = {"grade", "level", "near_node", "near_generation", "recommendation"}
_PRIOR_RUN_KEYS = {
    "run", "run_id", "metric", "best_metric", "run_best_metric", "similarity", "concepts",
    "matched_concepts", "outcomes", "matched_concept_outcomes", "source_receipt",
}
_STEERING_KEYS = {
    "kind", "ref", "source", "at_node", "node_id", "card_id", "concept_id", "strategy",
    "stance", "memo_id", "trigger", "value", "label", "operator", "reason",
}
_RECEIPT_KEYS = {
    "concept_evidence_nodes_total", "concept_evidence_nodes_incomplete", "concept_evidence_complete",
    "concepts_total", "concepts_omitted", "concepts_complete", "concept_outcomes_total",
    "concept_outcomes_omitted", "concept_outcomes_complete", "source_complete", "partial_capsules",
    "source_unknown_capsules", "source_concepts_omitted", "source_outcomes_omitted",
    "source_store_complete", "source_rows_total", "source_rows_quarantined", "source_malformed_rows",
    "source_invalid_capsule_rows", "source_duplicate_run_rows",
}
_CONCEPT_SOURCE_KEYS = {
    "source_complete", "partial_capsules", "source_unknown_capsules", "source_concepts_omitted",
    "source_outcomes_omitted", "source_store_complete", "source_rows_total",
    "source_rows_quarantined", "source_malformed_rows", "source_invalid_capsule_rows",
    "source_duplicate_run_rows",
}
# CODEX AGENT: this is the CARD's exact node/proposal owner receipt.  It is intentionally separate from
# `_CONCEPT_SOURCE_KEYS`, which describes cross-run capsule-store completeness inside cross_run_prior.
_CARD_CONCEPT_SOURCE_KINDS = frozenset({"card_added", "card_enriched", "node"})
_CARD_CONCEPT_PROVENANCE = frozenset({
    "researcher-authored", "classifier", "operator-edited", "offline-heuristic", "untrusted-source",
})


class PublicProjectionCount(BaseModel):
    """Exact count receipt for one bounded public projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    omitted: int = Field(ge=0)
    complete: bool

    @model_validator(mode="after")
    def _coherent_count(self) -> "PublicProjectionCount":
        if self.returned > self.total or self.omitted != self.total - self.returned:
            raise ValueError("projection counts must partition total")
        if self.complete and self.omitted:
            raise ValueError("a projection with omissions cannot be complete")
        return self


class PublicProjectionSlice(PublicProjectionCount):
    """Loss receipt for content units inside one public Card field."""

    unit: Literal["characters", "items", "entries", "fields", "values"]


class PublicCardProjectionReceipt(BaseModel):
    """Per-card field coverage; ``omissions`` is sparse and contains no source values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: bool
    fields: PublicProjectionCount
    omissions: dict[str, PublicProjectionSlice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent_card(self) -> "PublicCardProjectionReceipt":
        if self.complete != self.fields.complete:
            raise ValueError("card completeness must match its public-field receipt")
        if self.complete and self.omissions:
            raise ValueError("a complete card cannot carry omission receipts")
        return self


class PublicCardsProjectionMetadata(BaseModel):
    """Collection coverage for the backwards-compatible public ``cards`` mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_valid: bool
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    omitted: int = Field(ge=0)
    complete: bool
    items: dict[str, PublicCardProjectionReceipt] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent_collection(self) -> "PublicCardsProjectionMetadata":
        if self.returned > self.total or self.omitted != self.total - self.returned:
            raise ValueError("card collection counts must partition total")
        if self.returned != len(self.items):
            raise ValueError("every returned card must have one coverage receipt")
        exact = self.source_valid and not self.omitted and all(
            item.complete for item in self.items.values())
        if self.complete != exact:
            raise ValueError("collection completeness must be end-to-end")
        return self


class PublicCardsEnvelope(BaseModel):
    """Canonical Card fragment inserted into owner, SSE, and review state payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cards: dict[str, dict]
    cards_projection: PublicCardsProjectionMetadata


def _clip_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def _text(value, limit: int, *, free_text: bool = False):
    if not isinstance(value, str):
        return _SKIP
    from looplab.trust.redact import redact_secrets

    # CODEX AGENT: scan a bounded look-ahead so a credential crossing the display cut is redacted as one
    # token, while an attacker-sized prose field never costs O(raw size) on each SSE tick.
    bounded = value[:max(0, limit) + 512]
    try:
        return _clip_utf8(redact_secrets(bounded, entropy=free_text), limit)
    except UnicodeError:
        # CODEX AGENT: an unpaired surrogate cannot be represented by the UTF-8 JSON wire contract.
        # Omit it instead of letting one corrupt Card terminate every state/SSE/review projection.
        return _SKIP


def _number(value, *, integer: bool = False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _SKIP
    if integer and not isinstance(value, int):
        return _SKIP
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        return _SKIP
    if integer:
        return number if abs(number) <= (1 << 53) - 1 else _SKIP
    return number if math.isfinite(number) else _SKIP


def _refs(value, *, limit: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value[:limit]:
        ref = _text(item, _MAX_REF_BYTES)
        if ref is not _SKIP and ref and ref not in out:
            out.append(ref)
    return out


def _ints(value, *, limit: int = _MAX_ITEMS) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for item in value[:limit]:
        number = _number(item, integer=True)
        if number is not _SKIP and number is not None and number not in out:
            out.append(number)
    return out


def _bounded_scalar(value, *, free_text: bool = False):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _number(value, integer=True)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _text(value, _MAX_REF_BYTES, free_text=free_text)
    return _SKIP


def _named_scalars(value, allowed: set[str] | frozenset[str], *, free_text: bool = False):
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in sorted(allowed):
        if key not in value:
            continue
        bounded = _bounded_scalar(value[key], free_text=free_text)
        if bounded is not _SKIP:
            out[key] = bounded
    return out


def _params(value) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for raw_key in heapq.nsmallest(_MAX_ITEMS, value, key=str):
        key = _text(raw_key, 128)
        number = _number(value[raw_key])
        if key is not _SKIP and key and key not in out and number is not _SKIP and number is not None:
            out[key] = number
    return out


def _space(value) -> dict[str, list[float]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[float]] = {}
    for raw_key in heapq.nsmallest(_MAX_ITEMS, value, key=str):
        key = _text(raw_key, 128)
        raw_values = value[raw_key]
        if key is _SKIP or not key or key in out or not isinstance(raw_values, (list, tuple)):
            continue
        values = []
        for raw in raw_values[:_MAX_ITEMS]:
            number = _number(raw)
            if number is not _SKIP and number is not None:
                values.append(number)
        out[key] = values
    return out


def _outcomes(value) -> dict[str, float | None]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float | None] = {}
    for raw_key in heapq.nsmallest(_MAX_ITEMS, value, key=str):
        key = _text(raw_key, _MAX_REF_BYTES)
        number = _number(value[raw_key])
        if key is not _SKIP and key and key not in out and number is not _SKIP:
            out[key] = number
    return out


def _matched_outcomes(value) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        concept = _text(item.get("concept"), _MAX_REF_BYTES)
        retained = item.get("outcome_retained")
        outcome = _number(item.get("outcome"))
        row = {}
        if concept is not _SKIP:
            row["concept"] = concept
        if isinstance(retained, bool):
            row["outcome_retained"] = retained
        if outcome is not _SKIP:
            row["outcome"] = outcome
        out.append(row)
    return out


def _prior_run(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in sorted(_PRIOR_RUN_KEYS):
        if key not in value:
            continue
        raw = value[key]
        if key in {"concepts", "matched_concepts"}:
            out[key] = _refs(raw)
        elif key == "outcomes":
            out[key] = _outcomes(raw)
        elif key == "matched_concept_outcomes":
            out[key] = _matched_outcomes(raw)
        elif key == "source_receipt":
            out[key] = _named_scalars(raw, _RECEIPT_KEYS) or {}
        else:
            bounded = _bounded_scalar(raw)
            if bounded is not _SKIP:
                out[key] = bounded
    return out


def _cross_run(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    out = {}
    version = _number(value.get("v"), integer=True)
    if version is not _SKIP and version is not None:
        out["v"] = version
    out["matched_concepts"] = _refs(value.get("matched_concepts"))
    raw_runs = value.get("prior_runs")
    out["prior_runs"] = (
        [_prior_run(item) for item in raw_runs[:_MAX_ITEMS] if isinstance(item, dict)]
        if isinstance(raw_runs, (list, tuple)) else []
    )
    for key in ("prior_runs_total", "prior_runs_omitted"):
        number = _number(value.get(key), integer=True)
        if number is not _SKIP and number is not None:
            out[key] = number
    if isinstance(value.get("prior_runs_complete"), bool):
        out["prior_runs_complete"] = value["prior_runs_complete"]
    if isinstance(value.get("concept_source"), dict):
        out["concept_source"] = _named_scalars(value["concept_source"], _CONCEPT_SOURCE_KEYS) or {}
    return out


def _steering(value) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        row = {}
        for key in sorted(_STEERING_KEYS):
            if key not in item:
                continue
            raw = item[key]
            if isinstance(raw, (list, tuple)):
                row[key] = [bounded for child in raw[:_MAX_ITEMS]
                            if (bounded := _bounded_scalar(child, free_text=True)) is not _SKIP]
            else:
                bounded = _bounded_scalar(raw, free_text=True)
                if bounded is not _SKIP:
                    row[key] = bounded
        out.append(row)
    return out


def _exact_ref_projection(value) -> list[str] | None:
    """Return the exact public semantic set, or None when public bounds make it lossy."""
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_ITEMS:
        return None
    out: list[str] = []
    for item in value:
        ref = _text(item, _MAX_REF_BYTES)
        if ref is _SKIP or not ref or ref != item:
            return None
        if ref not in out:
            out.append(ref)
            if len(out) > _MAX_ITEMS:
                return None
    return out


def _card_concept_source(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return None if value is None else {}
    kind = value.get("kind")
    if kind not in _CARD_CONCEPT_SOURCE_KINDS:
        return {}
    out = {"kind": kind}
    if kind == "node":
        node_identity = []
        for key in ("node_id", "node_generation"):
            number = _number(value.get(key), integer=True)
            if number is not _SKIP and number is not None and number >= 0:
                out[key] = number
                node_identity.append(number)
        if len(node_identity) != 2:
            return {}
        provenance = value.get("provenance")
        if provenance in _CARD_CONCEPT_PROVENANCE:
            out["provenance"] = provenance
    provenance_known = kind != "node" or "provenance" in out
    membership_present = value.get("membership_present") is True
    claimed_complete = value.get("complete") is True
    claimed_receipt_valid = value.get("receipt_valid") is True
    raw_receipt = value.get("materialization_receipt")
    receipt = normalized_concept_materialization_receipt(raw_receipt) if raw_receipt is not None else None
    receipt_valid = claimed_receipt_valid and (raw_receipt is None or receipt is not None)
    out["membership_present"] = membership_present
    out["complete"] = bool(
        claimed_complete and membership_present and provenance_known
        and receipt_valid and receipt is None)
    out["receipt_valid"] = receipt_valid
    if receipt is not None:
        out["materialization_receipt"] = receipt
    return out


def _field(card, name: str):
    return card.get(name, _SKIP) if isinstance(card, dict) else getattr(card, name, _SKIP)


def _field_value(card, name: str):
    value = _field(card, name)
    if value is _SKIP:
        return _SKIP
    if name in _TEXT_LIMITS:
        return _text(value, _TEXT_LIMITS[name], free_text=True)
    if name in _REF_FIELDS:
        return None if value is None else _text(value, _MAX_REF_BYTES)
    if name in _INT_FIELDS:
        return _number(value, integer=True)
    if name in _FLOAT_FIELDS:
        return _number(value)
    if name in _REF_LIST_FIELDS:
        return _refs(value)
    if name in _INT_LIST_FIELDS:
        return _ints(value)
    if name == "actionable":
        return value if isinstance(value, bool) else _SKIP
    if name == "params":
        return _params(value)
    if name == "space":
        return _space(value)
    if name == "footprint":
        return _named_scalars(value, _FOOTPRINT_KEYS)
    if name == "novelty_verdict":
        return _named_scalars(value, _NOVELTY_KEYS, free_text=True)
    if name == "cross_run_prior":
        return _cross_run(value)
    if name == "concept_source":
        return _card_concept_source(value)
    if name == "steering_context":
        return _steering(value)
    return _SKIP


def _json_value(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _named_scalars_lossless(raw, bounded, allowed, *, free_text: bool = False) -> bool:
    if raw is None:
        return bounded is None
    if not isinstance(raw, dict):
        return False
    expected = {}
    for key in sorted(allowed):
        if key not in raw:
            continue
        value = _bounded_scalar(raw[key], free_text=free_text)
        if value is _SKIP or value != raw[key]:
            return False
        expected[key] = value
    return expected == bounded


def _outcomes_lossless(raw, bounded) -> bool:
    return (
        isinstance(raw, dict)
        and len(raw) <= _MAX_ITEMS
        and bounded == raw
    )


def _matched_outcomes_lossless(raw, bounded) -> bool:
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_ITEMS:
        return False
    expected = []
    for item in raw:
        if not isinstance(item, dict):
            return False
        row = {}
        if "concept" in item:
            concept = _text(item["concept"], _MAX_REF_BYTES)
            if concept is _SKIP or concept != item["concept"]:
                return False
            row["concept"] = concept
        if "outcome_retained" in item:
            if not isinstance(item["outcome_retained"], bool):
                return False
            row["outcome_retained"] = item["outcome_retained"]
        if "outcome" in item:
            outcome = _number(item["outcome"])
            if outcome is _SKIP or outcome != item["outcome"]:
                return False
            row["outcome"] = outcome
        expected.append(row)
    return expected == bounded


def _prior_run_lossless(raw, bounded) -> bool:
    if not isinstance(raw, dict) or _prior_run(raw) != bounded:
        return False
    for key in _PRIOR_RUN_KEYS & raw.keys():
        value = raw[key]
        if key in {"concepts", "matched_concepts"}:
            if (not isinstance(value, (list, tuple)) or len(value) > _MAX_ITEMS
                    or _refs(value) != list(value)):
                return False
        elif key == "outcomes":
            if not _outcomes_lossless(value, bounded.get(key)):
                return False
        elif key == "matched_concept_outcomes":
            if not _matched_outcomes_lossless(value, bounded.get(key)):
                return False
        elif key == "source_receipt":
            if not _named_scalars_lossless(value, bounded.get(key), _RECEIPT_KEYS):
                return False
        else:
            projected = _bounded_scalar(value)
            if projected is _SKIP or projected != value:
                return False
    return True


def _cross_run_lossless(raw, bounded) -> bool:
    if not isinstance(raw, dict) or _cross_run(raw) != bounded:
        return False
    if "v" in raw:
        version = _number(raw["v"], integer=True)
        if version is _SKIP or version != raw["v"]:
            return False
    if "matched_concepts" in raw:
        concepts = raw["matched_concepts"]
        if (not isinstance(concepts, (list, tuple)) or len(concepts) > _MAX_ITEMS
                or _refs(concepts) != list(concepts)):
            return False
    if "prior_runs" in raw:
        runs = raw["prior_runs"]
        if (not isinstance(runs, (list, tuple)) or len(runs) > _MAX_ITEMS
                or any(not isinstance(item, dict) for item in runs)):
            return False
        if not all(_prior_run_lossless(item, projected)
                   for item, projected in zip(runs, bounded["prior_runs"], strict=True)):
            return False
    for key in ("prior_runs_total", "prior_runs_omitted"):
        if key in raw:
            number = _number(raw[key], integer=True)
            if number is _SKIP or number != raw[key]:
                return False
    if "prior_runs_complete" in raw and not isinstance(raw["prior_runs_complete"], bool):
        return False
    if "concept_source" in raw and not _named_scalars_lossless(
            raw["concept_source"], bounded.get("concept_source"), _CONCEPT_SOURCE_KEYS):
        return False
    return True


def _steering_lossless(raw, bounded) -> bool:
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_ITEMS:
        return False
    expected = []
    for item in raw:
        if not isinstance(item, dict):
            return False
        row = {}
        for key in sorted(_STEERING_KEYS & item.keys()):
            value = item[key]
            if isinstance(value, (list, tuple)):
                if len(value) > _MAX_ITEMS:
                    return False
                children = []
                for child in value:
                    projected = _bounded_scalar(child, free_text=True)
                    if projected is _SKIP or projected != child:
                        return False
                    children.append(projected)
                row[key] = children
            else:
                projected = _bounded_scalar(value, free_text=True)
                if projected is _SKIP or projected != value:
                    return False
                row[key] = projected
        expected.append(row)
    return expected == bounded


def _concept_source_lossless(raw, bounded) -> bool:
    if not isinstance(raw, dict):
        return False
    allowed = {
        "kind", "node_id", "node_generation", "provenance", "membership_present",
        "complete", "receipt_valid", "materialization_receipt",
    }
    source_view = {key: raw[key] for key in allowed & raw.keys() if raw[key] is not None}
    return source_view == bounded == _card_concept_source(raw)


def _field_projection_lossless(name: str, raw, bounded) -> bool:
    raw = _json_value(raw)
    if name in _TEXT_LIMITS:
        return isinstance(raw, str) and bounded == raw
    if name in _REF_FIELDS:
        return isinstance(raw, str) and bounded == raw
    if name in _INT_FIELDS:
        return _number(raw, integer=True) == raw == bounded
    if name in _FLOAT_FIELDS:
        return _number(raw) == raw == bounded
    if name in _REF_LIST_FIELDS:
        return (
            isinstance(raw, (list, tuple))
            and len(raw) <= _MAX_ITEMS
            and _refs(raw) == list(raw) == bounded
        )
    if name in _INT_LIST_FIELDS:
        return (
            isinstance(raw, (list, tuple))
            and len(raw) <= _MAX_ITEMS
            and _ints(raw) == list(raw) == bounded
        )
    if name == "actionable":
        return isinstance(raw, bool) and bounded is raw
    if name == "params":
        return isinstance(raw, dict) and len(raw) <= _MAX_ITEMS and bounded == raw
    if name == "space":
        return isinstance(raw, dict) and len(raw) <= _MAX_ITEMS and bounded == raw
    if name == "footprint":
        return _named_scalars_lossless(raw, bounded, _FOOTPRINT_KEYS)
    if name == "novelty_verdict":
        return _named_scalars_lossless(raw, bounded, _NOVELTY_KEYS, free_text=True)
    if name == "cross_run_prior":
        return _cross_run_lossless(raw, bounded)
    if name == "concept_source":
        return _concept_source_lossless(raw, bounded)
    if name == "steering_context":
        return _steering_lossless(raw, bounded)
    return False


def _field_exact(card, dto: dict, name: str) -> bool:
    raw = _field(card, name)
    if raw is _SKIP or raw is None or name not in dto:
        return False
    bounded = _field_value(card, name)
    return (
        bounded is not _SKIP
        and dto[name] == bounded
        and _field_projection_lossless(name, raw, bounded)
    )


def _field_slice(name: str, raw, projected, *, exact: bool) -> PublicProjectionSlice:
    raw = _json_value(raw)
    if isinstance(raw, str):
        unit = "characters"
        total = len(raw)
        if exact:
            returned = total
        elif isinstance(projected, str):
            # CODEX AGENT: redaction/clipping preserves order but may transform a suffix. Count only
            # the exact prefix; stopping at the first changed character cannot overstate coverage.
            returned = next(
                (index for index, (source, public) in enumerate(zip(raw, projected))
                 if source != public),
                min(len(raw), len(projected)),
            )
        else:
            returned = 0
    elif isinstance(raw, (list, tuple)):
        unit = "items"
        total = len(raw)
        if exact:
            returned = total
        elif isinstance(projected, list):
            # CODEX AGENT: count only source values that survived byte clipping/redaction exactly.
            # The projector considers at most `_MAX_ITEMS`, so this stays bounded even for a hostile list.
            remaining = list(raw[:_MAX_ITEMS])
            returned = 0
            for item in projected:
                try:
                    index = remaining.index(item)
                except ValueError:
                    continue
                returned += 1
                remaining.pop(index)
        else:
            returned = 0
    elif isinstance(raw, dict):
        unit = "entries"
        keys = None
        if name == "footprint":
            keys = _FOOTPRINT_KEYS & raw.keys()
        elif name == "novelty_verdict":
            keys = _NOVELTY_KEYS & raw.keys()
        elif name == "cross_run_prior":
            keys = {
                "v", "matched_concepts", "prior_runs", "prior_runs_total",
                "prior_runs_omitted", "prior_runs_complete", "concept_source",
            } & raw.keys()
        elif name == "concept_source":
            keys = {
                "kind", "node_id", "node_generation", "provenance", "membership_present",
                "complete", "receipt_valid", "materialization_receipt",
            } & raw.keys()
            keys = {key for key in keys if raw[key] is not None}
        if keys is None:
            total = len(raw)
            if exact:
                returned = total
            elif isinstance(projected, dict):
                returned = sum(
                    key in raw and raw[key] == value
                    for key, value in projected.items()
                )
            else:
                returned = 0
        else:
            total = len(keys)
            returned = total if exact else sum(
                key in projected and projected[key] == raw[key]
                for key in keys
                if isinstance(projected, dict)
            )
    else:
        unit = "values"
        total = 1
        returned = int(exact)
    return PublicProjectionSlice(
        unit=unit,
        total=total,
        returned=returned,
        omitted=total - returned,
        complete=exact,
    )


def _card_projection_receipt(card, dto: dict) -> PublicCardProjectionReceipt:
    # CODEX AGENT: the mapping key, not a spoofable object field, is the authoritative public Card.id.
    total = 1
    returned = 1
    exact_fields: dict[str, bool] = {"id": True}
    omissions: dict[str, PublicProjectionSlice] = {}
    for name in _FIELDS[1:]:
        raw = _field(card, name)
        if raw is _SKIP or raw is None:
            continue
        total += 1
        exact = _field_exact(card, dto, name)
        exact_fields[name] = exact
        if exact:
            returned += 1
            continue
        projected = dto.get(name, _SKIP)
        omissions[name] = _field_slice(name, raw, projected, exact=False)

    for group, names in {
        "action": ("operator", "params", "space", "eval_profile"),
        "concepts": ("concept_tags", "concept_source", "provenance_tier"),
    }.items():
        present = [
            name for name in names
            if (value := _field(card, name)) is not _SKIP and value is not None
        ]
        exact_count = sum(exact_fields.get(name, False) for name in present)
        if present and exact_count != len(present):
            omissions[group] = PublicProjectionSlice(
                unit="fields",
                total=len(present),
                returned=exact_count,
                omitted=len(present) - exact_count,
                complete=False,
            )

    fields = PublicProjectionCount(
        total=total,
        returned=returned,
        omitted=total - returned,
        complete=returned == total,
    )
    return PublicCardProjectionReceipt(
        complete=fields.complete,
        fields=fields,
        omissions=omissions,
    )


def _dto(card, authoritative_id: str) -> dict:
    # CODEX AGENT: fixed admission order keeps identity/lifecycle available; rich optional fields enter
    # only while the complete UTF-8 JSON representation remains inside the per-card SSE envelope.
    out: dict = {"id": authoritative_id}
    concept_source_claimed_complete = False
    for name in _FIELDS[1:]:
        value = _field_value(card, name)
        if value is _SKIP:
            continue
        if name == "concept_source" and isinstance(value, dict):
            concept_source_claimed_complete = value.get("complete") is True
            # CODEX AGENT: source completeness is end-to-end on the public DTO. Start fail-closed and
            # restore True only after the exact tag set itself survives size/ref projection below.
            value = {**value, "complete": False}
        candidate = {**out, name: value}
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= PUBLIC_CARD_MAX_BYTES:
            out = candidate
    concept_source = out.get("concept_source")
    if isinstance(concept_source, dict):
        exact_tags = _exact_ref_projection(_field(card, "concept_tags"))
        if (concept_source_claimed_complete and exact_tags is not None
                and out.get("concept_tags", _SKIP) == exact_tags):
            concept_source["complete"] = True  # false -> true only shrinks the bounded JSON envelope
        # CODEX AGENT: never ship two disagreeing provenance claims.  The exact node receipt is the
        # authority; proposal-only/malformed/size-omitted receipts clear the compatibility scalar.
        exact_provenance = (
            concept_source.get("provenance") if concept_source.get("kind") == "node" else None)
        if exact_provenance in _CARD_CONCEPT_PROVENANCE:
            candidate = {**out, "provenance_tier": exact_provenance}
            candidate_bytes = json.dumps(
                candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(candidate_bytes) <= PUBLIC_CARD_MAX_BYTES:
                out = candidate
            else:
                out.pop("provenance_tier", None)
        else:
            out.pop("provenance_tier", None)
    else:
        out.pop("provenance_tier", None)
    return out


def _authoritative_id(value) -> bool:
    if not isinstance(value, str) or not value or not value.isprintable():
        return False
    try:
        bounded = len(value.encode("utf-8")) <= _MAX_REF_BYTES
    except UnicodeError:
        return False
    if not bounded:
        return False
    # CODEX AGENT: a public key must remain the exact durable key. Secret redaction, clipping, or any
    # other rewrite omits the card fail-closed instead of publishing it under a new identity.
    return _text(value, _MAX_REF_BYTES) == value


def _selection_int(card, name: str):
    value = _number(_field(card, name), integer=True)
    return value if value is not _SKIP and value is not None and value >= 0 else None


def _selection_key(cards: dict, card_id: str):
    card = cards[card_id]
    status = _field(card, "status")
    actionable = _field(card, "actionable") is True
    active = isinstance(status, str) and status in {"proposed", "building", "coded", "running"}
    priority = _selection_int(card, "priority")
    foresight = _selection_int(card, "foresight_rank")
    rank = priority if priority is not None else foresight
    scored = _selection_int(card, "scored_against")
    created = _selection_int(card, "created_at_node")
    freshness = max(value for value in (scored, created, 0) if value is not None)
    return (
        0 if active or actionable else 1,
        0 if active else 1,
        0 if actionable else 1,
        rank is None,
        rank if rank is not None else 0,
        -freshness,
        card_id,
    )


def _projection_metadata(source_valid: bool, total: int,
                         admitted: list[tuple[str, dict, PublicCardProjectionReceipt]]
                         ) -> PublicCardsProjectionMetadata:
    items = {card_id: receipt for card_id, _dto_value, receipt in sorted(admitted)}
    returned = len(items)
    omitted = total - returned
    complete = source_valid and not omitted and all(item.complete for item in items.values())
    return PublicCardsProjectionMetadata(
        source_valid=source_valid,
        total=total,
        returned=returned,
        omitted=omitted,
        complete=complete,
        items=items,
    )


def public_cards_projection(cards) -> PublicCardsEnvelope:
    """Return the one canonical, bounded Card wire fragment plus exact coverage metadata."""
    source_valid = isinstance(cards, dict)
    if not source_valid:
        metadata = _projection_metadata(False, 0, [])
        return PublicCardsEnvelope(cards={}, cards_projection=metadata)

    total = len(cards)
    valid_ids = (key for key in cards if _authoritative_id(key))
    selected = heapq.nsmallest(
        PUBLIC_CARD_MAX_COUNT, valid_ids, key=lambda card_id: _selection_key(cards, card_id))
    admitted: list[tuple[str, dict, PublicCardProjectionReceipt]] = []
    board_bytes = 2
    metadata_items_bytes = 2
    for card_id in selected:
        dto = _dto(cards[card_id], card_id)
        receipt = _card_projection_receipt(cards[card_id], dto)
        key_bytes = json.dumps(card_id, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        dto_bytes = json.dumps(dto, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry_bytes = len(key_bytes) + 1 + len(dto_bytes) + bool(admitted)
        receipt_bytes = json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        metadata_entry_bytes = len(key_bytes) + 1 + len(receipt_bytes) + bool(admitted)
        # CODEX AGENT: metadata is part of the public surface, not an unbounded diagnostic sidecar.
        # Admit a card only when both its DTO and its exact receipt fit their independently fixed budgets.
        if (board_bytes + entry_bytes > PUBLIC_CARDS_MAX_BYTES
                or metadata_items_bytes + metadata_entry_bytes
                > PUBLIC_CARDS_PROJECTION_MAX_BYTES - 1_024):
            continue
        admitted.append((card_id, dto, receipt))
        board_bytes += entry_bytes
        metadata_items_bytes += metadata_entry_bytes

    metadata = _projection_metadata(True, total, admitted)
    metadata_bytes = len(json.dumps(
        metadata.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8"))
    # CODEX AGENT: keep the limit executable even though admission reserves more than today's fixed
    # envelope, so a future metadata field cannot silently invalidate the SSE byte bound.
    while admitted and metadata_bytes > PUBLIC_CARDS_PROJECTION_MAX_BYTES:
        admitted.pop()
        metadata = _projection_metadata(True, total, admitted)
        metadata_bytes = len(json.dumps(
            metadata.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))

    # CODEX AGENT: relevance decides admission, while the wire mapping is id-sorted for exact replay and
    # cache determinism independent of source insertion order.
    public = {card_id: dto for card_id, dto, _receipt in sorted(admitted)}
    return PublicCardsEnvelope(cards=public, cards_projection=metadata)


def public_cards(cards) -> dict[str, dict]:
    """Backwards-compatible mapping view of :func:`public_cards_projection`."""
    return public_cards_projection(cards).cards
