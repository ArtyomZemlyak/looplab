"""Bounded public wire projection for the derived hypothesis-card board."""
from __future__ import annotations

import heapq
import json
import math


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
_MAX_ITEMS = 32
_MAX_TEXT_BYTES = 2_048
_MAX_REF_BYTES = 256
_SKIP = object()

# CODEX AGENT: this is the explicit wire DTO. Adding a Card or event field does not publish it until this
# boundary is reviewed; fixed order also makes byte output deterministic across event/mapping order.
_FIELDS = (
    "id", "status", "verdict", "actionable", "statement", "seed_statement", "source",
    "created_at_node", "rationale", "evidence", "best_delta", "merged_into", "aliases",
    "dropped_reason", "dropped_by", "parent_id", "parent_ids", "scored_against", "operator",
    "params", "space", "eval_profile", "concept_tags", "priority", "foresight_rank", "confidence",
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


def _clip_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def _text(value, limit: int, *, free_text: bool = False):
    if not isinstance(value, str):
        return _SKIP
    from looplab.trust.redact import redact_secrets

    # CODEX AGENT: scan a bounded look-ahead so a credential crossing the display cut is redacted as one
    # token, while an attacker-sized prose field never costs O(raw size) on each SSE tick.
    bounded = value[:max(0, limit) + 512]
    return _clip_utf8(redact_secrets(bounded, entropy=free_text), limit)


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
    if name == "steering_context":
        return _steering(value)
    return _SKIP


def _dto(card, authoritative_id: str) -> dict:
    # CODEX AGENT: fixed admission order keeps identity/lifecycle available; rich optional fields enter
    # only while the complete UTF-8 JSON representation remains inside the per-card SSE envelope.
    out: dict = {"id": authoritative_id}
    for name in _FIELDS[1:]:
        value = _field_value(card, name)
        if value is _SKIP:
            continue
        candidate = {**out, name: value}
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= PUBLIC_CARD_MAX_BYTES:
            out = candidate
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


def public_cards(cards) -> dict[str, dict]:
    """Return a deterministic, allow-listed and size-bounded card mapping."""
    if not isinstance(cards, dict):
        return {}
    valid_ids = (key for key in cards if _authoritative_id(key))
    selected = heapq.nsmallest(
        PUBLIC_CARD_MAX_COUNT, valid_ids, key=lambda card_id: _selection_key(cards, card_id))
    admitted: list[tuple[str, dict]] = []
    board_bytes = 2
    for card_id in selected:
        dto = _dto(cards[card_id], card_id)
        key_bytes = json.dumps(card_id, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        dto_bytes = json.dumps(dto, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry_bytes = len(key_bytes) + 1 + len(dto_bytes) + bool(admitted)
        if board_bytes + entry_bytes > PUBLIC_CARDS_MAX_BYTES:
            continue
        admitted.append((card_id, dto))
        board_bytes += entry_bytes
    # CODEX AGENT: relevance decides admission, while the wire mapping is id-sorted for exact replay and
    # cache determinism independent of source insertion order.
    return {card_id: dto for card_id, dto in sorted(admitted)}
