"""Receipt-aware, lifecycle-aware concept projections for agent-facing consumers.

Replay is append-only and may materialize an unresolved delta dependency as an empty list.  Consumers
must cross the lifecycle and materialization-receipt boundary before describing that list as a real empty
classification or using it as proposal inheritance.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Optional

from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED,
                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                                 NODE_CONCEPT_PROVENANCE_OPERATOR, valid_concept_id)

from looplab.core.concepts import (
    normalize_concept_id as _core_normalize_concept_id,
    normalized_concept_materialization_receipt as _core_normalized_receipt,
    normalized_concept_renames as _core_normalized_renames,
    resolve_concept as _core_resolve_concept,
)


ProjectionStatus = Literal["complete", "partial", "unavailable"]
_MISSING = object()
_EXACT_INHERITANCE_PROVENANCE = frozenset({
    NODE_CONCEPT_PROVENANCE_AUTHORED,
    NODE_CONCEPT_PROVENANCE_CLASSIFIER,
    NODE_CONCEPT_PROVENANCE_OPERATOR,
    NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
})
_UNKNOWN_PARENT_MEMBERSHIP_REASON = "delta_dependency_unknown_parent_membership"


def _normalized_id(raw: Any) -> Optional[str]:
    value = _core_normalize_concept_id(raw)
    return value if isinstance(value, str) and valid_concept_id(value) else None


def canonical_recorded_concept(
    raw: Any, rename: dict[str, Optional[str]],
) -> tuple[Optional[str], Optional[str]]:
    """Strictly resolve one recorded id through the canonical bounded identity projection."""
    concept_id, reason = _core_resolve_concept(raw, rename)
    return concept_id, str(reason) if reason is not None else None


def _rename_projection(raw: Any) -> tuple[dict[str, Optional[str]], set[str]]:
    if raw is None:
        return {}, set()
    if not isinstance(raw, dict):
        return {}, {"invalid_consolidation_map"}
    reasons: set[str] = set()
    normalized = _core_normalized_renames(raw)
    # Use the helper's OWN corruption signals, not a len() heuristic. A benign consolidation where two
    # raw spellings normalize to the SAME source id (e.g. 'reg dropout' and 'reg-dropout') collapses
    # the map BY DESIGN — the helper resolves such duplicate spellings deterministically and does not
    # flag them. Only a source/target that fails to canonicalize (`endpoint_problem`) or a non-dict map
    # (`problem`) is genuine identity corruption. The old `len(projected) != len(raw)` check treated
    # every legitimate merge as corruption, poisoning global_reasons run-wide so every node_concept_delta
    # returned unavailable and delta_safe was False for the whole run.
    if getattr(normalized, "problem", None) is not None or getattr(normalized, "endpoint_problem", False):
        reasons.add("invalid_consolidation_map")
    projected = dict(normalized)

    if any(target is None for target in projected.values()):
        reasons.add("invalid_consolidation_map")
    # Rename corruption is global identity corruption even when a particular selected parent does not
    # happen to contain the poisoned source.  Validate every source once through the shared resolver.
    for source in projected:
        _resolved, problem = canonical_recorded_concept(source, projected)
        if problem:
            reasons.add(problem)
    return projected, reasons


def _materialization_receipt(raw: Any) -> Optional[tuple[ProjectionStatus, tuple[str, ...]]]:
    """Normalize a canonical receipt envelope through the core owner's exact typed validation.

    ONLY that envelope crosses this boundary. In particular a bare reason string — even a RECOGNIZED
    one — is malformed here and fails closed: a receipt that lost its status is not evidence that the
    materialization was merely partial.
    """
    normalized = _core_normalized_receipt(raw)
    if not isinstance(normalized, dict):
        return None
    status = normalized.get("status")
    reasons = normalized.get("reasons")
    if status not in ("partial", "unavailable") or not isinstance(reasons, (list, tuple)):
        return None
    if not reasons or any(not isinstance(reason, str) or not reason for reason in reasons):
        return None
    return status, tuple(reasons)


@dataclass(frozen=True)
class CurrentConceptProjection:
    status: ProjectionStatus
    reasons: tuple[str, ...]
    global_reasons: tuple[str, ...]
    memberships: dict[int, tuple[str, ...]]
    trusted_memberships: dict[int, tuple[str, ...]]
    active_nodes: frozenset[int]
    available_nodes: frozenset[int]
    absent_nodes: frozenset[int]
    unavailable_nodes: dict[int, tuple[str, ...]]
    partial_nodes: dict[int, tuple[str, ...]]
    receipt_nodes: frozenset[int]
    run_base: tuple[str, ...]
    run_base_status: ProjectionStatus
    run_base_reasons: tuple[str, ...]
    rename: dict[str, Optional[str]]

    def node_status(self, node_id: int) -> tuple[ProjectionStatus, tuple[str, ...]]:
        if node_id not in self.active_nodes:
            return "unavailable", ("inactive_or_missing_experiment",)
        if node_id in self.unavailable_nodes:
            return "unavailable", self.unavailable_nodes[node_id]
        if node_id in self.absent_nodes:
            return "unavailable", ("membership_not_recorded",)
        if node_id in self.partial_nodes:
            return "partial", self.partial_nodes[node_id]
        if node_id in self.available_nodes:
            return "complete", ()
        return "unavailable", ("membership_unavailable",)


# Reasons that mean classification is simply PENDING (the data will still arrive), NOT that the concept
# store is corrupt. When these are the ONLY reasons, an all-pending run (0 available nodes) is PARTIAL, not
# "unavailable" — the latter would conflate a fresh, still-classifying run with a genuinely broken store.
_PENDING_REASONS = frozenset({"membership_not_recorded"})


def _status(reasons: set[str], available_count: int, *, fatal: bool = False) -> ProjectionStatus:
    if fatal:
        return "unavailable"
    # Genuine corruption with nothing usable left is unavailable; pending-only reasons never are (a run
    # whose nodes are still being classified is incomplete, not broken).
    if (reasons - _PENDING_REASONS) and available_count == 0:
        return "unavailable"
    return "partial" if reasons else "complete"


def current_concept_projection(state: Any) -> CurrentConceptProjection:
    """Return strict CURRENT memberships plus explicit availability receipts.

    A real ``RunState`` always has ``nodes``.  A missing attribute is tolerated for lightweight read-tool
    fixtures and treats integer membership keys as pseudo-current nodes; a present malformed node map
    fails closed.
    """
    rename, rename_reasons = _rename_projection(getattr(state, "concept_consolidation", None))
    reasons = set(rename_reasons)
    global_reasons = set(rename_reasons)
    fatal = False

    raw_memberships = getattr(state, "node_concepts", None)
    if raw_memberships is None:
        raw_memberships = {}
    if not isinstance(raw_memberships, dict):
        raw_memberships = {}
        reasons.add("invalid_membership_map")
        global_reasons.add("invalid_membership_map")
        fatal = True

    raw_nodes = getattr(state, "nodes", _MISSING)
    if raw_nodes is _MISSING:
        known_nodes = {
            node_id for node_id in raw_memberships
            if isinstance(node_id, int) and not isinstance(node_id, bool)
        }
        active_nodes = set(known_nodes)
    elif not isinstance(raw_nodes, dict):
        known_nodes = set()
        active_nodes = set()
        reasons.add("invalid_node_map")
        global_reasons.add("invalid_node_map")
        fatal = True
    else:
        known_nodes = {
            node_id for node_id in raw_nodes
            if isinstance(node_id, int) and not isinstance(node_id, bool)
        }
        if len(known_nodes) != len(raw_nodes):
            reasons.add("invalid_node_map")
            global_reasons.add("invalid_node_map")
        raw_aborted = getattr(state, "aborted_nodes", None)
        if raw_aborted is None:
            aborted: set[int] = set()
        elif isinstance(raw_aborted, (list, tuple, set, frozenset)):
            aborted = {
                node_id for node_id in raw_aborted
                if isinstance(node_id, int) and not isinstance(node_id, bool)
            }
            if len(aborted) != len(raw_aborted):
                reasons.add("invalid_lifecycle_state")
                global_reasons.add("invalid_lifecycle_state")
        else:
            aborted = set()
            reasons.add("invalid_lifecycle_state")
            global_reasons.add("invalid_lifecycle_state")
            fatal = True
        active_nodes = {
            node_id for node_id in known_nodes
            if node_id not in aborted and not getattr(raw_nodes[node_id], "tombstoned", False)
        }

    unavailable: dict[int, set[str]] = {}
    partial_nodes: dict[int, set[str]] = {}
    receipt_nodes: set[int] = set()
    raw_receipts = getattr(state, "node_concept_materialization_receipts", None)
    if raw_receipts is None:
        raw_receipts = {}
    if not isinstance(raw_receipts, dict):
        reasons.add("invalid_concept_materialization_receipt")
        global_reasons.add("invalid_concept_materialization_receipt")
        fatal = True
        for node_id in active_nodes:
            unavailable.setdefault(node_id, set()).add("invalid_concept_materialization_receipt")
    else:
        for node_id, raw_receipt in raw_receipts.items():
            if (isinstance(node_id, bool) or not isinstance(node_id, int)
                    or node_id not in known_nodes):
                reasons.add("invalid_concept_materialization_receipt")
                global_reasons.add("invalid_concept_materialization_receipt")
                continue
            receipt = _materialization_receipt(raw_receipt)
            if receipt is None:
                # valid historical receipts follow a deleted node out of the CURRENT
                # projection, but a malformed durable row is not a receipt and remains global source
                # corruption. Validate before the lifecycle filter, matching ConceptFrame exactly.
                reasons.add("invalid_concept_materialization_receipt")
                global_reasons.add("invalid_concept_materialization_receipt")
                if node_id in active_nodes:
                    unavailable.setdefault(node_id, set()).add(
                        "invalid_concept_materialization_receipt")
                continue
            if node_id not in active_nodes:
                continue
            receipt_status, receipt_reasons = receipt
            receipt_nodes.add(node_id)
            reasons.update(receipt_reasons)
            if receipt_status == "unavailable":
                unavailable.setdefault(node_id, set()).update(receipt_reasons)
            else:
                partial_nodes.setdefault(node_id, set()).update(receipt_reasons)

    memberships: dict[int, tuple[str, ...]] = {}
    available_nodes: set[int] = set()
    for node_id, raw_ids in raw_memberships.items():
        if (isinstance(node_id, bool) or not isinstance(node_id, int)
                or node_id not in known_nodes):
            reasons.add("invalid_experiment_reference")
            global_reasons.add("invalid_experiment_reference")
            continue
        if node_id not in active_nodes or node_id in unavailable:
            continue
        if not isinstance(raw_ids, (list, tuple)):
            reasons.add("invalid_membership_list")
            unavailable.setdefault(node_id, set()).add("invalid_membership_list")
            continue
        canonical: set[str] = set()
        for raw_id in raw_ids:
            concept_id, problem = canonical_recorded_concept(raw_id, rename)
            if problem:
                reasons.add(problem)
                partial_nodes.setdefault(node_id, set()).add(problem)
                continue
            if concept_id is not None:
                canonical.add(concept_id)
        memberships[node_id] = tuple(sorted(canonical))
        available_nodes.add(node_id)

    trusted_nodes = set(available_nodes)
    raw_provenance = getattr(state, "node_concept_provenance", _MISSING)
    if raw_provenance is not _MISSING:
        if not isinstance(raw_provenance, dict):
            reasons.add("invalid_concept_provenance_map")
            global_reasons.add("invalid_concept_provenance_map")
            for node_id in available_nodes:
                trusted_nodes.discard(node_id)
                partial_nodes.setdefault(node_id, set()).add(
                    _UNKNOWN_PARENT_MEMBERSHIP_REASON)
        else:
            for node_id in available_nodes:
                if raw_provenance.get(node_id) not in _EXACT_INHERITANCE_PROVENANCE:
                    # display can retain the strict valid subset, but unknown authorship
                    # cannot certify exact inheritance or feed a supposedly exact cross-run overlap.
                    reasons.add(_UNKNOWN_PARENT_MEMBERSHIP_REASON)
                    trusted_nodes.discard(node_id)
                    partial_nodes.setdefault(node_id, set()).add(
                        _UNKNOWN_PARENT_MEMBERSHIP_REASON)

    # exact/trusted membership is a subset of nodes whose local projection is COMPLETE.
    # A canonical partial receipt, or a malformed retained raw id discovered without a receipt, means the
    # displayed valid subset is useful but incomplete.  It must not authorize exact inheritance or
    # cross-run overlap merely because its producer provenance is otherwise trusted.
    trusted_nodes.difference_update(partial_nodes)

    absent_nodes = active_nodes - available_nodes - set(unavailable)
    if absent_nodes:
        # absence is classification-pending, not an honest empty membership. Keep this
        # node-local (so a clean selected parent can still author delta) while broad tools report PARTIAL.
        reasons.add("membership_not_recorded")

    raw_base = getattr(state, "run_base_concepts", None)
    if raw_base is None:
        raw_base = []
    base_reasons = set(rename_reasons)
    base_fatal = False
    if not isinstance(raw_base, (list, tuple)):
        raw_base = []
        base_reasons.add("invalid_run_base_concepts")
        base_fatal = True
    canonical_base: set[str] = set()
    for raw_id in raw_base:
        concept_id, problem = canonical_recorded_concept(raw_id, rename)
        if problem:
            base_reasons.add(problem)
            continue
        if concept_id is not None:
            canonical_base.add(concept_id)
    raw_base_receipt = getattr(state, "run_base_concept_receipt", _MISSING)
    base_receipt_status: Optional[ProjectionStatus] = None
    if raw_base_receipt is not _MISSING and raw_base_receipt is not None:
        base_receipt = _materialization_receipt(raw_base_receipt)
        if base_receipt is None:
            base_reasons.add("invalid_concept_materialization_receipt")
            base_fatal = True
        else:
            base_receipt_status, receipt_reasons = base_receipt
            base_reasons.update(receipt_reasons)
    base_evidence = 1 if canonical_base or (not raw_base and not base_reasons) else 0
    base_status = _status(base_reasons, base_evidence, fatal=base_fatal)
    if base_receipt_status == "unavailable":
        base_status = "unavailable"
        canonical_base.clear()
    elif base_receipt_status == "partial" and not base_fatal:
        base_status = "partial"

    return CurrentConceptProjection(
        status=_status(reasons, len(available_nodes), fatal=fatal),
        reasons=tuple(sorted(reasons)),
        global_reasons=tuple(sorted(global_reasons)),
        memberships=dict(sorted(memberships.items())),
        trusted_memberships={
            node_id: memberships[node_id] for node_id in sorted(trusted_nodes)},
        active_nodes=frozenset(active_nodes),
        available_nodes=frozenset(available_nodes),
        absent_nodes=frozenset(absent_nodes),
        unavailable_nodes={key: tuple(sorted(value)) for key, value in sorted(unavailable.items())},
        partial_nodes={key: tuple(sorted(value)) for key, value in sorted(partial_nodes.items())},
        receipt_nodes=frozenset(receipt_nodes),
        run_base=tuple(sorted(canonical_base)),
        run_base_status=base_status,
        run_base_reasons=tuple(sorted(base_reasons)),
        rename=rename,
    )


def canonical_concept_query(state: Any, raw: Any) -> Optional[str]:
    projection = current_concept_projection(state)
    canonical, _problem = canonical_recorded_concept(raw, projection.rename)
    return canonical


def concept_inheritance_context(state: Any, parent_id: Optional[int]) -> dict[str, Any]:
    """Build trusted metadata plus bounded-renderable untrusted recorded inheritance values."""
    projection = current_concept_projection(state)
    if parent_id is None:
        primary_status = projection.run_base_status
        primary_reasons = projection.run_base_reasons
        inherited = projection.run_base
    else:
        primary_status, primary_reasons = projection.node_status(parent_id)
        inherited = projection.memberships.get(parent_id, ()) if primary_status != "unavailable" else ()
    # only actual inheritance inputs and globally malformed stores/identity maps gate authoring.
    # A receipt on an unrelated branch still makes broad tools PARTIAL, but cannot disable a clean parent.
    delta_safe = (
        not projection.global_reasons
        and projection.run_base_status == "complete"
        and primary_status == "complete"
    )
    return {
        "projection_status": projection.status,
        "projection_reasons": list(projection.reasons),
        "global_reasons": list(projection.global_reasons),
        "run_base_status": projection.run_base_status,
        "run_base_reasons": list(projection.run_base_reasons),
        "run_base": list(projection.run_base),
        "primary_parent_id": parent_id,
        "primary_membership_status": primary_status,
        "primary_reasons": list(primary_reasons),
        "primary_inherited": list(inherited),
        "delta_safe": delta_safe,
    }


def bounded_untrusted_concept_json(context: dict[str, Any], *, max_chars: int = 3_500) -> str:
    """JSON-quote recorded ids without slicing inside an id or producing invalid JSON."""
    max_chars = max(800, min(int(max_chars), 8_000))
    payload = {key: value for key, value in context.items()
               if key not in {"run_base", "primary_inherited"}}
    payload.update({
        "run_base": [],
        "run_base_total": len(context.get("run_base") or []),
        "primary_inherited": [],
        "primary_inherited_total": len(context.get("primary_inherited") or []),
        "omitted_recorded_ids": 0,
    })

    def encoded(candidate: dict[str, Any]) -> str:
        return json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    omitted = 0
    for key in ("run_base", "primary_inherited"):
        values = context.get(key) or []
        for index, value in enumerate(values):
            candidate = {**payload, key: [*payload[key], value]}
            if len(encoded(candidate)) > max_chars:
                omitted += len(values) - index
                break
            payload = candidate
    payload["omitted_recorded_ids"] = omitted
    rendered = encoded(payload)
    # Fixed metadata is a closed vocabulary and normally far below the minimum cap. Keep a valid-JSON
    # fallback rather than slicing a JSON string if a future reason list grows unexpectedly.
    if len(rendered) > max_chars:
        rendered = encoded({
            "projection_status": context.get("projection_status", "unavailable"),
            "delta_safe": False,
            "omitted_recorded_ids": (
                len(context.get("run_base") or []) + len(context.get("primary_inherited") or [])),
            "receipt": "concept_context_metadata_cap",
        })
    return rendered
