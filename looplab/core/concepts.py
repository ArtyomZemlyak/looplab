"""Canonical concept identity and materialization failure contracts.

This module is deliberately below replay, search, and serve.  Those layers all consume the same
bounded identity resolver, so a rename cannot mean one thing in durable state and another thing in
the public ConceptFrame.
"""
from __future__ import annotations

from typing import Any, Iterable, Literal, TypeAlias, cast
from typing_extensions import TypedDict


MAX_CONCEPT_ID_CHARS = 256
MAX_CONCEPT_ID_DEPTH = 12
CONCEPT_RENAME_HOP_CAP = 16
MAX_MATERIALIZED_CONCEPTS = 64

ConceptMaterializationReason = Literal[
    "concept_mode_unsupported",
    "concepts_per_node_cap",
    "delta_dependency_cycle",
    "delta_dependency_missing_parent",
    "delta_dependency_unknown_parent_membership",
    "invalid_consolidation_map",
    "invalid_concept_id",
    "rename_cycle",
    "rename_hop_cap",
]
ConceptMaterializationStatus = Literal["partial", "unavailable"]


class ConceptMaterializationReceipt(TypedDict):
    """Bounded derived receipt for one node's effective membership."""

    status: ConceptMaterializationStatus
    reasons: list[ConceptMaterializationReason]

CONCEPT_MODE_UNSUPPORTED_REASON: ConceptMaterializationReason = "concept_mode_unsupported"
CONCEPTS_PER_NODE_CAP_REASON: ConceptMaterializationReason = "concepts_per_node_cap"
CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON: ConceptMaterializationReason = "delta_dependency_cycle"
CONCEPT_DELTA_MISSING_PARENT_REASON: ConceptMaterializationReason = (
    "delta_dependency_missing_parent"
)
CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON: ConceptMaterializationReason = (
    "delta_dependency_unknown_parent_membership"
)
CONCEPT_INVALID_CONSOLIDATION_MAP_REASON: ConceptMaterializationReason = "invalid_consolidation_map"
CONCEPT_INVALID_ID_REASON: ConceptMaterializationReason = "invalid_concept_id"
CONCEPT_RENAME_CYCLE_REASON: ConceptMaterializationReason = "rename_cycle"
CONCEPT_RENAME_HOP_CAP_REASON: ConceptMaterializationReason = "rename_hop_cap"

CONCEPT_MATERIALIZATION_REASONS = frozenset({
    CONCEPT_MODE_UNSUPPORTED_REASON,
    CONCEPTS_PER_NODE_CAP_REASON,
    CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
    CONCEPT_DELTA_MISSING_PARENT_REASON,
    CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON,
    CONCEPT_INVALID_CONSOLIDATION_MAP_REASON,
    CONCEPT_INVALID_ID_REASON,
    CONCEPT_RENAME_CYCLE_REASON,
    CONCEPT_RENAME_HOP_CAP_REASON,
})


def concept_materialization_reason(raw: Any) -> ConceptMaterializationReason | None:
    """Validate a public receipt value without raising or guessing future strings."""
    return (cast(ConceptMaterializationReason, raw)
            if isinstance(raw, str) and raw in CONCEPT_MATERIALIZATION_REASONS else None)

# Multiple invalid parents can reach a merge.  A fixed public priority makes its one compact receipt
# independent of parent/event insertion order while descendants still inherit the exact selected cause.
_REASON_PRIORITY = {
    reason: rank for rank, reason in enumerate((
        CONCEPT_MODE_UNSUPPORTED_REASON,
        CONCEPT_DELTA_MISSING_PARENT_REASON,
        CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON,
        CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
        CONCEPT_INVALID_CONSOLIDATION_MAP_REASON,
        CONCEPT_RENAME_CYCLE_REASON,
        CONCEPT_RENAME_HOP_CAP_REASON,
        CONCEPT_INVALID_ID_REASON,
        CONCEPTS_PER_NODE_CAP_REASON,
    ))
}

_UNAVAILABLE_REASONS = frozenset({
    CONCEPT_MODE_UNSUPPORTED_REASON,
    CONCEPT_DELTA_MISSING_PARENT_REASON,
    CONCEPT_DELTA_UNKNOWN_PARENT_MEMBERSHIP_REASON,
    CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
    CONCEPT_INVALID_CONSOLIDATION_MAP_REASON,
})


def select_concept_materialization_reason(
    reasons: Iterable[ConceptMaterializationReason],
) -> ConceptMaterializationReason | None:
    """Choose one deterministic public receipt from one or more root causes."""
    unique = set(reasons)
    return min(unique, key=lambda reason: (_REASON_PRIORITY.get(reason, 1_000), reason)) if unique else None


def concept_materialization_receipt(
    reasons: Iterable[ConceptMaterializationReason],
) -> ConceptMaterializationReceipt | None:
    """Build the canonical bounded envelope for a set of validated root causes."""
    ordered = sorted(set(reasons), key=lambda reason: (_REASON_PRIORITY.get(reason, 1_000), reason))
    if not ordered:
        return None
    return {
        "status": "unavailable" if any(reason in _UNAVAILABLE_REASONS for reason in ordered)
        else "partial",
        "reasons": ordered[:len(CONCEPT_MATERIALIZATION_REASONS)],
    }


def normalized_concept_materialization_receipt(raw: Any) -> ConceptMaterializationReceipt | None:
    """Validate and canonicalize an untrusted public receipt envelope, or return ``None``."""
    if not isinstance(raw, dict) or set(raw) != {"status", "reasons"}:
        return None
    raw_reasons = raw.get("reasons")
    if not isinstance(raw_reasons, list) or not raw_reasons:
        return None
    reasons: list[ConceptMaterializationReason] = []
    for raw_reason in raw_reasons:
        reason = concept_materialization_reason(raw_reason)
        if reason is None:
            return None
        reasons.append(reason)
    canonical = concept_materialization_receipt(reasons)
    return canonical if canonical is not None and raw == canonical else None


def normalize_concept_id(raw: Any) -> str | None:
    """Return the single bounded canonical spelling of a concept id, or ``None``.

    A valid id is ``segment[/segment...]``.  Segments contain unicode letters/digits plus ``-._``
    and at least one letter/digit.  Normalization is intentionally small and stable: trim, lowercase,
    replace ordinary spaces with ``-``, and trim surrounding slashes.
    """
    if not isinstance(raw, str) or len(raw) > MAX_CONCEPT_ID_CHARS:
        return None
    value = raw.strip().lower().replace(" ", "-").strip("/")
    if not value or len(value) > MAX_CONCEPT_ID_CHARS:
        return None
    parts = value.split("/")
    if len(parts) > MAX_CONCEPT_ID_DEPTH:
        return None
    if any(
        not part
        or not any(char.isalnum() for char in part)
        or any(not (char.isalnum() or char in "-._") for char in part)
        for part in parts
    ):
        return None
    return value


def valid_concept_id(raw: Any) -> bool:
    """Whether ``raw`` has a valid bounded concept identity."""
    return normalize_concept_id(raw) is not None


class BoundedConceptAccumulator:
    """Lexical top-K set with O(limit) memory for wide/deep DAG unions."""

    def __init__(self, *, limit: int = MAX_MATERIALIZED_CONCEPTS):
        self.limit = max(0, int(limit))
        self.values: set[str] = set()
        self.overflow = False

    def add(self, value: str) -> None:
        if value in self.values:
            return
        if len(self.values) < self.limit:
            self.values.add(value)
            return
        self.overflow = True
        if self.limit and value < max(self.values):
            self.values.remove(max(self.values))
            self.values.add(value)

    def update(self, values: Iterable[str]) -> None:
        for value in values:
            self.add(value)


def bounded_raw_concept_values(
    values: Any, *, limit: int = MAX_MATERIALIZED_CONCEPTS,
) -> tuple[list[str], bool, bool]:
    """Heal one untrusted list into bounded valid raw spellings plus cap/invalid flags."""
    if not isinstance(values, list):
        return [], False, True
    limit = max(0, int(limit))
    invalid = False
    if len(values) <= limit:
        valid: list[str] = []
        for raw in values:
            if normalize_concept_id(raw) is None:
                invalid = True
            else:
                valid.append(raw)
        return valid, False, invalid
    selected = BoundedConceptAccumulator(limit=limit)
    raw_by_id: dict[str, str] = {}
    for raw in values:
        canonical = normalize_concept_id(raw)
        if canonical is None:
            invalid = True
            continue
        evicted = (max(selected.values) if canonical not in selected.values
                   and len(selected.values) >= selected.limit and selected.limit else None)
        selected.add(canonical)
        if canonical in selected.values:
            raw_by_id.setdefault(canonical, raw)
        if evicted is not None and evicted not in selected.values:
            raw_by_id.pop(evicted, None)
    return [raw_by_id[canonical] for canonical in sorted(selected.values)], selected.overflow, invalid


class NormalizedConceptRenames(dict[str, str | None]):
    """Marker type preventing repeated normalization in bounded inner loops.

    ``None`` is retained for an invalid target.  Dropping that row would silently turn a broken alias
    into an apparently canonical identity; the resolver instead returns ``invalid_concept_id``.
    """

    problem: ConceptMaterializationReason | None = None
    endpoint_problem: bool = False


ConceptRenameMap: TypeAlias = dict[str, Any] | NormalizedConceptRenames


def normalized_concept_renames(raw_renames: Any) -> NormalizedConceptRenames:
    """Normalize a consolidation map deterministically without mutating its audit copy."""
    if isinstance(raw_renames, NormalizedConceptRenames):
        return raw_renames
    out = NormalizedConceptRenames()
    if raw_renames is None:
        return out
    if not isinstance(raw_renames, dict):
        out.problem = CONCEPT_INVALID_CONSOLIDATION_MAP_REASON
        return out
    candidates: dict[str, set[str]] = {}
    invalid_sources: set[str] = set()
    for raw_source, raw_target in raw_renames.items():
        source = normalize_concept_id(raw_source)
        if source is None:
            out.endpoint_problem = True
            continue
        target = normalize_concept_id(raw_target)
        if target is None:
            out.endpoint_problem = True
            invalid_sources.add(source)
        else:
            candidates.setdefault(source, set()).add(target)
    for source in sorted(candidates.keys() | invalid_sources):
        valid = candidates.get(source)
        # Valid duplicate spellings win deterministically.  If every target is malformed, retain a
        # poison marker so all consumers expose the same typed failure instead of ignoring the rename.
        out[source] = min(valid) if valid else None
    return out


def resolve_concept(
    raw: Any,
    renames: ConceptRenameMap | Any = None,
    *,
    hop_cap: int = CONCEPT_RENAME_HOP_CAP,
) -> tuple[str | None, ConceptMaterializationReason | None]:
    """Normalize one id and resolve a bounded, cycle-safe consolidation chain."""
    rename = normalized_concept_renames(renames)
    if rename.problem is not None:
        return None, rename.problem
    current = normalize_concept_id(raw)
    if current is None:
        return None, CONCEPT_INVALID_ID_REASON
    seen: set[str] = set()
    for _hop in range(max(0, hop_cap) + 1):
        if current in seen:
            return None, CONCEPT_RENAME_CYCLE_REASON
        seen.add(current)
        if current not in rename:
            return current, None
        target = rename[current]
        if target is None:
            return None, CONCEPT_INVALID_ID_REASON
        current = target
    return None, CONCEPT_RENAME_HOP_CAP_REASON


def resolve_concept_set(
    values: Any,
    renames: ConceptRenameMap | Any = None,
    *,
    limit: int = MAX_MATERIALIZED_CONCEPTS,
) -> tuple[set[str], ConceptMaterializationReason | None]:
    """Resolve a concept collection into a deterministic bounded set."""
    resolved, problems = resolve_concept_set_reasons(values, renames, limit=limit)
    return resolved, select_concept_materialization_reason(problems)


def resolve_concept_set_reasons(
    values: Any,
    renames: ConceptRenameMap | Any = None,
    *,
    limit: int = MAX_MATERIALIZED_CONCEPTS,
) -> tuple[set[str], set[ConceptMaterializationReason]]:
    """Detailed set resolver retaining all closed problems for a receipt envelope."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set(), {CONCEPT_INVALID_ID_REASON}
    rename = normalized_concept_renames(renames)
    resolved: set[str] = set()
    problems: set[ConceptMaterializationReason] = set()
    limit = max(0, int(limit))
    for raw in values:
        canonical, problem = resolve_concept(raw, rename)
        if problem is not None:
            problems.add(problem)
        elif canonical is not None:
            if canonical in resolved:
                continue
            if len(resolved) < limit:
                resolved.add(canonical)
            else:
                problems.add(CONCEPTS_PER_NODE_CAP_REASON)
                if limit and canonical < max(resolved):
                    resolved.remove(max(resolved))
                    resolved.add(canonical)
    return resolved, problems


def bounded_concept_set(
    values: Iterable[str], *, limit: int = MAX_MATERIALIZED_CONCEPTS,
) -> tuple[set[str], bool]:
    """Keep the lexicographically smallest distinct identities with bounded memory."""
    selected = BoundedConceptAccumulator(limit=limit)
    selected.update(values)
    return selected.values, selected.overflow
