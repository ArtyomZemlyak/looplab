"""Bounded, versioned ConceptFrame projection helpers.

Both the GET read and the paid derived-lens endpoint use this module so neither path can bypass
canonicalization, source caps, lifecycle references, or completeness receipts.
"""
from __future__ import annotations

from bisect import bisect_left
import math
import unicodedata
from typing import Optional

from fastapi import HTTPException

from looplab.core.models import CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON, valid_concept_id
from looplab.serve.protocol import RUN_GENERATION_FIELD


CONCEPT_FRAME_SCHEMA = 1
MAX_NODE_MEMBERSHIPS = 2_048
MAX_CONCEPTS_PER_NODE = 64
MAX_MEMBERSHIPS = 8_192
MAX_EDGE_WEIGHT = MAX_MEMBERSHIPS
MAX_TREE_NODES = 4_096
MAX_EDGES = 2_048
MAX_EDGE_ENDPOINTS = 4_096
MAX_ID_CHARS = 256
MAX_ID_DEPTH = 12
MAX_RENAME_HOPS = 16
MAX_LENS_RELS = 8
MAX_LENS_RELS_CHARS = 192
MAX_LENS_PROMPT_BYTES = 2_048
MAX_LENS_PROMPT_CHARS = 800
MAX_LENS_BODY_BYTES = 4_096

# The completeness reasons that are MONOTONE TRUNCATION CAPS: the source data was otherwise valid but
# exceeded a size bound, so the bounded frame is a faithful (if partial) substrate. Callers that must
# distinguish "safe to serve/mint against the partial frame" from "torn/malformed source" (the POST
# lens-mint gate in serve/routers/runs.py) use THIS explicit set, NOT an `endswith("_cap")` heuristic:
# `rename_hop_cap` ends in "_cap" but is a corruption-adjacent UNRESOLVED-IDENTITY signal (a rename chain
# over MAX_RENAME_HOPS drops the concept), classified with its sibling `rename_cycle` as blocking. Every
# reason NOT in this set is corruption-class (torn log / malformed agent-emitted data) and must block.
TRUNCATION_CAP_REASONS = frozenset({
    "node_membership_cap", "concepts_per_node_cap", "membership_cap", "tree_node_cap",
    "edge_cap", "edge_endpoint_cap", "experiment_ref_cap",
})


def concept_id(raw) -> Optional[str]:
    """Return a projection-safe canonical concept id, or ``None``."""
    if not isinstance(raw, str) or len(raw) > MAX_ID_CHARS:
        return None
    value = raw.strip().lower().replace(" ", "-").strip("/")
    if not value or len(value) > MAX_ID_CHARS:
        return None
    parts = value.split("/")
    if (len(parts) > MAX_ID_DEPTH or any(not part for part in parts)
            or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)
            # Enforce the shared CHARSET rule (letters/digits + -._ per segment): reject base64/hash
            # garbage, symbols, emoji, `<script>`, `a/..` that the depth/length/control checks miss.
            or not valid_concept_id(value)):
        return None
    return value


def canonical_concept(raw, rename: dict) -> tuple[Optional[str], Optional[str]]:
    """Resolve a bounded rename chain; malformed identity is omitted and receipt-stamped."""
    current = raw
    seen: set[str] = set()
    for _hop in range(MAX_RENAME_HOPS + 1):
        canonical = concept_id(current)
        if canonical is None:
            return None, "invalid_concept_id"
        if canonical in seen:
            return None, "rename_cycle"
        seen.add(canonical)
        nxt = rename.get(current)
        if nxt is None and current != canonical:
            nxt = rename.get(canonical)
        if nxt is None:
            return canonical, None
        current = nxt
    return None, "rename_hop_cap"


def finite_metric(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _current_nodes(state) -> dict[int, object]:
    """Nodes eligible for the current operational projection.

    Replay deliberately retains tombstoned and aborted nodes (and their concept memberships) as
    append-only history.  ConceptFrame is a current-state read, so lifecycle deletion is applied here,
    at the projection boundary, without mutating that folded audit record.
    """
    nodes = getattr(state, "nodes", None) or {}
    if not isinstance(nodes, dict):
        return {}
    raw_aborted = getattr(state, "aborted_nodes", None) or []
    aborted = set(raw_aborted) if isinstance(raw_aborted, (list, tuple, set, frozenset)) else set()
    return {
        node_id: node for node_id, node in nodes.items()
        if node_id not in aborted and not getattr(node, "tombstoned", False)
    }


def normalized_custom_lens_name(raw) -> Optional[str]:
    """Canonical identity for one ephemeral, client-replayed derived lens."""
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    name = "-".join(raw.strip().lower().replace("_", "-").split())
    if not name or len(name) > 64 or not name[0].isalnum():
        return None
    if any(not (char.isascii() and (char.isalnum() or char == "-")) for char in name):
        return None
    return name


def bounded_lens_label(raw, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    label = " ".join(raw.split())
    if not label or any(unicodedata.category(char).startswith("C") for char in label):
        return fallback
    return label[:60]


def lens_request(lens: str, rels: Optional[str], lens_pack: list[dict]) -> tuple[str, dict, str]:
    """Validate either one shipped lens or a bounded ephemeral replay spec."""
    registered = {item.get("name"): item for item in lens_pack
                  if isinstance(item, dict) and isinstance(item.get("name"), str)}
    if rels is None:
        if lens not in registered:
            raise HTTPException(400, {
                "code": "concept_lens_unknown",
                "requested_lens": lens,
                "allowed_lenses": sorted(registered),
                "message": "The requested concept lens is not registered.",
            })
        return lens, dict(registered[lens]), "shipped"

    name = normalized_custom_lens_name(lens)
    if name is None or name in registered:
        raise HTTPException(400, {
            "code": "concept_lens_name_invalid",
            "message": "A derived lens needs a bounded non-reserved ASCII slug.",
        })
    if not rels or len(rels) > MAX_LENS_RELS_CHARS:
        raise HTTPException(400, {
            "code": "concept_lens_relations_invalid",
            "message": "A derived lens needs a bounded non-empty relation subset.",
        })
    raw_relations = rels.split(",")
    if len(raw_relations) > MAX_LENS_RELS or any(not relation.strip() for relation in raw_relations):
        raise HTTPException(400, {
            "code": "concept_lens_relations_invalid",
            "message": "Derived lens relations contain an empty or oversized subset.",
        })
    allowed_relations = {relation for item in registered.values()
                         for relation in (item.get("rels") or []) if isinstance(relation, str)}
    relations = list(dict.fromkeys(relation.strip() for relation in raw_relations))
    unknown = sorted(set(relations) - allowed_relations)
    if unknown:
        raise HTTPException(400, {
            "code": "concept_lens_relation_unknown",
            "unknown_relations": unknown,
            "allowed_relations": sorted(allowed_relations),
            "message": "Every derived-lens relation must come from the shipped registry.",
        })
    return name, {"name": name, "label": name, "rels": relations,
                  "kind": "path" if relations == ["is_a"] else "edge"}, "ephemeral-validated"


_EDGE_PROV_RANK = {"asserted": 2, "evidenced": 1}


def edge_rank(edge):
    provenance = str(edge.get("provenance") or "")
    return (float(edge.get("confidence") or 0.0),
            _EDGE_PROV_RANK.get(provenance, 0), provenance)


def _retain_smallest(selected: dict, ordered_keys: list, key, value, limit: int, *, rank=None) -> bool:
    """Retain the ``limit`` smallest distinct keys using memory bounded by that limit.

    Returns whether one distinct valid key had to be omitted (or evicted). A duplicate never consumes
    capacity; callers may provide a total ``rank`` to retain the best value for a selected key.
    """
    limit = max(0, limit)
    if key in selected:
        if rank is not None and rank(value) > rank(selected[key]):
            selected[key] = value
        return False
    if not limit:
        return True
    if len(ordered_keys) >= limit and key >= ordered_keys[-1]:
        return True
    omitted = False
    if len(ordered_keys) >= limit:
        evicted = ordered_keys.pop()
        del selected[evicted]
        omitted = True
    ordered_keys.insert(bisect_left(ordered_keys, key), key)
    selected[key] = value
    return omitted


def bounded_inputs(state, lens_pack: list[dict]) -> dict:
    """Canonical bounded inputs shared by GET projection and paid lens derivation."""
    from looplab.search.concept_graph import concept_touch_counts, graph_from_node_concepts

    registered = {item.get("name"): item for item in lens_pack
                  if isinstance(item, dict) and isinstance(item.get("name"), str)}
    registered_rels = {rel for item in registered.values() for rel in (item.get("rels") or [])
                       if isinstance(rel, str)}
    reasons: set[str] = set()
    rename = getattr(state, "concept_consolidation", None) or {}
    if not isinstance(rename, dict):
        rename = {}
        reasons.add("invalid_consolidation_map")
    raw_memberships = getattr(state, "node_concepts", None) or {}
    if not isinstance(raw_memberships, dict):
        raw_memberships = {}
        reasons.add("invalid_membership_map")

    current_nodes = _current_nodes(state)

    # CODEX AGENT: [] is otherwise indistinguishable from an explicitly authored known-empty set.
    # Replay stamps every unresolved delta-cycle member/descendant, and the public frame carries that
    # as a corruption-class reason so the UI cannot present fail-closed emptiness as authoritative.
    # Apply the same CURRENT lifecycle boundary as memberships: tombstoned/aborted historical receipts
    # remain visible in a historical prefix but do not poison today's projection.
    raw_materialization_receipts = getattr(
        state, "node_concept_materialization_receipts", None)
    if raw_materialization_receipts is None:
        raw_materialization_receipts = {}
    if not isinstance(raw_materialization_receipts, dict):
        reasons.add("invalid_concept_materialization_receipt")
    else:
        for raw_node_id, raw_reason in raw_materialization_receipts.items():
            if (isinstance(raw_node_id, bool) or not isinstance(raw_node_id, int)
                    or raw_node_id not in state.nodes
                    or raw_reason != CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON):
                reasons.add("invalid_concept_materialization_receipt")
                continue
            if raw_node_id in current_nodes:
                reasons.add(CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON)

    # CODEX AGENT: insertion order is replay history, not semantic priority. Each level keeps a bounded
    # lexicographic top-K while scanning the full source for integrity receipts. Empty/invalid-only and
    # deleted lifecycle rows have no canonical membership, so they cannot consume a live node slot.
    # The resulting memory is O(MAX_NODE_MEMBERSHIPS * MAX_CONCEPTS_PER_NODE), while the chosen paid-lens
    # input remains byte-identical across replay insertion orders.
    membership_rows: dict[int, list[str]] = {}
    membership_node_ids: list[int] = []
    node_membership_overflow = False
    for raw_node_id, raw_ids in raw_memberships.items():
        if (isinstance(raw_node_id, bool) or not isinstance(raw_node_id, int)
                or raw_node_id not in state.nodes):
            reasons.add("invalid_experiment_reference")
            continue
        if raw_node_id not in current_nodes:
            continue
        if not isinstance(raw_ids, list):
            reasons.add("invalid_membership_list")
            continue
        canonical_ids: dict[str, None] = {}
        ordered_concepts: list[str] = []
        concepts_per_node_overflow = False
        for raw_concept in raw_ids:
            canonical, problem = canonical_concept(raw_concept, rename)
            if problem:
                reasons.add(problem)
                continue
            concepts_per_node_overflow |= _retain_smallest(
                canonical_ids, ordered_concepts, canonical, None, MAX_CONCEPTS_PER_NODE)
        if concepts_per_node_overflow:
            reasons.add("concepts_per_node_cap")
        if not ordered_concepts:
            continue
        node_membership_overflow |= _retain_smallest(
            membership_rows, membership_node_ids, raw_node_id, ordered_concepts,
            MAX_NODE_MEMBERSHIPS)
    if node_membership_overflow:
        reasons.add("node_membership_cap")

    memberships: dict[int, list[str]] = {}
    accepted_concepts: set[str] = set()
    accepted_prefixes: set[str] = set()
    membership_count = 0
    membership_overflow = False
    for raw_node_id in membership_node_ids:
        node_concepts: set[str] = set()
        for canonical in membership_rows[raw_node_id]:
            if membership_count >= MAX_MEMBERSHIPS:
                membership_overflow = True
                continue
            parts = canonical.split("/")
            prefixes = {"/".join(parts[:depth]) for depth in range(1, len(parts) + 1)}
            if len(accepted_prefixes | prefixes) > MAX_TREE_NODES:
                reasons.add("tree_node_cap")
                continue
            accepted_prefixes.update(prefixes)
            accepted_concepts.add(canonical)
            node_concepts.add(canonical)
            membership_count += 1
        if node_concepts:
            memberships[raw_node_id] = sorted(node_concepts)
    if membership_overflow:
        reasons.add("membership_cap")

    graph, tags = graph_from_node_concepts(memberships)
    concept_ids = sorted(accepted_concepts)
    raw_edges = getattr(state, "concept_edges", None) or {}
    if not isinstance(raw_edges, dict):
        raw_edges = {}
        reasons.add("invalid_edge_map")
    # CODEX AGENT: canonical edge identity is the cap ordering key. Retain the bounded smallest distinct
    # tuples while scanning: raw aliases, duplicates and legacy co_occurs rows neither consume capacity
    # nor create a false truncation receipt. A retained duplicate still uses edge_rank's total winner.
    canonical_edges: dict[tuple[str, str, str], dict] = {}
    canonical_edge_keys: list[tuple[str, str, str]] = []
    edge_overflow = False
    for edge in raw_edges.values():
        if not isinstance(edge, dict):
            reasons.add("invalid_edge")
            continue
        relation = edge.get("rel")
        if relation == "co_occurs":
            # CODEX AGENT: co-occurrence is a materialized observation of CURRENT memberships, not an
            # immutable assertion. Legacy max-folded receipts cannot represent a lower count/removal
            # after re-tagging, so they are audit history only; the live projection is derived below.
            continue
        src, src_problem = canonical_concept(edge.get("src"), rename)
        dst, dst_problem = canonical_concept(edge.get("dst"), rename)
        confidence = finite_metric(edge.get("confidence"))
        # CODEX AGENT: ``confidence`` is the replay field for both normalized confidence and the
        # integer co-occurrence evidence emitted by strategy.py. Bound the weight by the largest
        # self-contained evidence receipt instead of silently discarding every repeated pairing.
        # REVIEW(2026-07-16): the MAX_EDGE_WEIGHT bound fixes the count>=2 rejection, but a run whose
        # one pair co-occurs on MORE than MAX_EDGE_WEIGHT nodes still trips "invalid_edge" — and the
        # max-wins fold means that once recorded, the reason never clears (same permanence as before,
        # just a higher threshold). At the cap the edge is CLAMPED to MAX_EDGE_WEIGHT (the evidence
        # saturates), not rejected — rejection would convert the run's strongest empirical edge into a
        # permanent partial/non-authoritative frame. Only a NEGATIVE/None/malformed weight is invalid.
        if (src_problem or dst_problem or not src or not dst or src == dst
                or not isinstance(relation, str) or relation not in registered_rels or confidence is None
                or confidence < 0.0):
            reasons.add(src_problem or dst_problem or "invalid_edge")
            continue
        # CODEX AGENT: IEEE signed zero compares equal but has different JSON bytes. Normalize it before
        # duplicate ranking/storage so -0.0 versus 0.0 arrival order cannot change the cached core or the
        # exact paid-lens input derived from it (legacy/manual states may bypass the replay sanitizer).
        if confidence == 0.0:
            confidence = 0.0
        if confidence > MAX_EDGE_WEIGHT:
            confidence = float(MAX_EDGE_WEIGHT)
        provenance = edge.get("provenance") or ""
        if (not isinstance(provenance, str) or len(provenance) > 64
                or any(unicodedata.category(char).startswith("C") for char in provenance)):
            reasons.add("invalid_edge")
            continue
        key = (src, relation, dst)
        candidate = {"src": src, "rel": relation, "dst": dst,
                     "confidence": confidence, "provenance": provenance}
        edge_overflow |= _retain_smallest(
            canonical_edges, canonical_edge_keys, key, candidate, MAX_EDGES, rank=edge_rank)
    if edge_overflow:
        reasons.add("edge_cap")

    edges: dict[tuple[str, str, str], dict] = {}
    edge_endpoints: set[str] = set()
    for key in canonical_edge_keys:
        candidate = canonical_edges[key]
        src, _relation, dst = key
        new_endpoints = {src, dst} - edge_endpoints
        # One global projection-node budget covers BOTH tag/path nodes and edge-only endpoints.
        if (len(edge_endpoints) + len(new_endpoints) > MAX_EDGE_ENDPOINTS
                or len(accepted_prefixes | edge_endpoints | new_endpoints) > MAX_TREE_NODES):
            reasons.add("edge_endpoint_cap")
            continue
        edge_endpoints.update(new_endpoints)
        edges[key] = candidate

    derived_edge_count = 0

    def add_derived(src: str, relation: str, dst: str, confidence: float,
                    provenance: str) -> bool:
        """Add one deterministic edge within the same public edge/node budgets."""
        nonlocal derived_edge_count
        key = (src, relation, dst)
        if key in edges:
            return True
        if len(edges) >= MAX_EDGES:
            reasons.add("edge_cap")
            return False
        new_endpoints = {src, dst} - edge_endpoints
        if (len(edge_endpoints) + len(new_endpoints) > MAX_EDGE_ENDPOINTS
                or len(accepted_prefixes | edge_endpoints | new_endpoints) > MAX_TREE_NODES):
            reasons.add("edge_endpoint_cap")
            return True
        edge_endpoints.update(new_endpoints)
        edges[key] = {"src": src, "rel": relation, "dst": dst,
                      "confidence": confidence, "provenance": provenance}
        derived_edge_count += 1
        return True

    # CODEX AGENT: co-occurrence is a projection of the bounded CURRENT membership snapshot.
    # Recompute it on every core build for online, offline and legacy runs alike; persisting monotone
    # max receipts made stale weights and removed pairs impossible to retract.
    if "co_occurs" in registered_rels:
        pair_counts: dict[tuple[str, str], int] = {}
        static_capacity_full = len(edges) >= MAX_EDGES
        omitted_derived = False
        for node_concepts in memberships.values():
            cs = sorted(set(node_concepts))
            for i in range(len(cs)):
                for j in range(i + 1, len(cs)):
                    pair = (cs[i], cs[j])
                    pair_counts[pair] = pair_counts.get(pair, 0) + 1
                    # CODEX AGENT: a full static edge budget still has to inspect the bounded membership
                    # snapshot. The second observation proves that one valid derived edge was omitted;
                    # only then is edge_cap truthful. Stop immediately because no derived edge can fit.
                    if static_capacity_full and pair_counts[pair] == 2:
                        reasons.add("edge_cap")
                        omitted_derived = True
                        break
                if omitted_derived:
                    break
            if omitted_derived:
                break
        if not static_capacity_full:
            for (src, dst), cnt in sorted(pair_counts.items(), key=lambda kv: (-kv[1], kv[0])):
                if cnt < 2:
                    continue
                if not add_derived(
                        src, "co_occurs", dst, float(min(cnt, MAX_EDGE_WEIGHT)),
                        "co-tag (derived)"):
                    break

    return {
        "memberships": memberships,
        "concept_ids": concept_ids,
        "edges": edges,
        "touch": concept_touch_counts(memberships),
        "graph": graph,
        "tags": tags,
        "reasons": reasons,
        "membership_count": membership_count,
        "source_membership_nodes": len(raw_memberships),
        "source_edges": len(raw_edges),
        "derived_edges": derived_edge_count,
    }


def folded_concepts(state):
    """Compatibility seam for pure callers; shares the bounded public materialization rules."""
    from looplab.search.concept_graph import default_lenses

    inputs = bounded_inputs(state, default_lenses())
    return (inputs["memberships"], inputs["concept_ids"], inputs["edges"], inputs["touch"])


def build_core(state, *, run_id: str, lens_pack: list[dict],
               generation: Optional[str], requested_seq: Optional[int], captured_seq: int,
               max_seq: int, source_divergence: Optional[dict]) -> dict:
    """Build the bounded, lens-independent core for one exact folded event prefix.

    The returned object is deliberately an internal transport shape: every collection retained in it
    is bounded by this module's public ConceptFrame limits, so a small process-local snapshot cache can
    retain it without retaining the (potentially much larger) folded ``RunState``.
    """
    from looplab.search.concept_graph import concept_metrics

    inputs = bounded_inputs(state, lens_pack)
    memberships = inputs["memberships"]
    concept_ids = inputs["concept_ids"]
    edges = inputs["edges"]
    touch = inputs["touch"]
    graph = inputs["graph"]
    tags = inputs["tags"]
    reasons = set(inputs["reasons"])
    if source_divergence is not None:
        reasons.add("event_log_corruption")
    if not generation:
        reasons.add("generation_unavailable")

    # Metrics (including the all-experiment median baseline) must use the same current lifecycle
    # projection as memberships. A shallow Pydantic copy preserves the complete folded state for
    # history while preventing deleted nodes from skewing deltas even when they no longer have tags.
    metric_state = state.model_copy(update={"nodes": _current_nodes(state)})
    metrics = concept_metrics(metric_state, graph, tags)
    if metrics.get("baseline") is not None and finite_metric(metrics["baseline"]) is None:
        metrics["baseline"] = None
        reasons.add("nonfinite_metric")
    for bucket in ("rows", "rollup"):
        for row in (metrics.get(bucket) or {}).values():
            for field in ("best", "mean", "worst", "delta_best", "delta_mean"):
                value = row.get(field)
                if value is not None and finite_metric(value) is None:
                    row[field] = None
                    reasons.add("nonfinite_metric")

    best = state.best()
    best_id = best.id if best is not None else None
    provenance_counts: dict[str, int] = {}
    experiment_refs: dict[str, list[dict]] = {canonical: [] for canonical in concept_ids}
    reference_count = 0
    for node_id in sorted(memberships):
        node = state.nodes[node_id]
        metric = finite_metric(node.robust_metric)
        if node.robust_metric is not None and metric is None:
            reasons.add("nonfinite_metric")
        provenance = str((getattr(state, "node_concept_provenance", None) or {}).get(node_id)
                         or "unknown")
        status = getattr(node.status, "value", str(node.status))
        for canonical in memberships[node_id]:
            if reference_count >= MAX_MEMBERSHIPS:
                reasons.add("experiment_ref_cap")
                break
            # A self-contained lifecycle ref prevents a historical frame from joining CURRENT /state
            # after the node or same-id run has moved to a different generation.
            experiment_refs[canonical].append({
                "node_id": node_id,
                "node_generation": int(node.attempt),
                "metric": metric,
                "metric_kind": "robust_metric",
                "status": status,
                "feasible": node.feasible if isinstance(node.feasible, bool) else None,
                "is_best": node_id == best_id,
                "membership_provenance": provenance,
            })
            provenance_counts[provenance] = provenance_counts.get(provenance, 0) + 1
            reference_count += 1

    source_authoritative = source_divergence is None and bool(generation)
    return {
        "run_id": run_id,
        RUN_GENERATION_FIELD: generation or None,
        "requested_seq": requested_seq,
        "captured_seq": captured_seq,
        "max_seq": max_seq,
        "concept_ids": concept_ids,
        "edges": edges,
        "touch": touch,
        "metrics": metrics,
        "experiment_refs": experiment_refs,
        "reasons": tuple(sorted(reasons)),
        "source_authoritative": source_authoritative,
        "source_membership_nodes": inputs["source_membership_nodes"],
        "source_edges": inputs["source_edges"],
        "derived_edges": inputs["derived_edges"],
        "included_membership_nodes": len(memberships),
        "membership_count": inputs["membership_count"],
        "reference_count": reference_count,
        "provenance_counts": dict(sorted(provenance_counts.items())),
        "source_integrity": ({"complete": True, "generation_identified": bool(generation)}
                             if source_divergence is None else {
                                 "complete": False,
                                 "generation_identified": bool(generation),
                                 "corrupt_line": source_divergence.get("corrupt_line"),
                                 "dropped_lines": source_divergence.get("dropped_lines"),
                             }),
    }


def core_lens_inputs(core: dict) -> dict:
    """Return the already-bounded vocabulary used by the paid lens minting prompt."""
    return {"concept_ids": core["concept_ids"], "edges": core["edges"]}


def project_frame(core: dict, *, requested_lens: str, lens_pack: list[dict],
                  requested_spec: Optional[dict] = None,
                  lens_registration: str = "shipped") -> dict:
    """Pure lens-specific projection over one immutable-by-convention bounded core."""
    from looplab.search.concept_graph import project_hierarchy, project_lens

    registered = {item.get("name"): item for item in lens_pack
                  if isinstance(item, dict) and isinstance(item.get("name"), str)}
    requested_spec = dict(requested_spec or registered[requested_lens])
    concept_ids = core["concept_ids"]
    edges = core["edges"]
    touch = core["touch"]
    requested_relations = set(requested_spec.get("rels") or [])
    lens_edges = {key: edge for key, edge in edges.items()
                  if edge.get("rel") in requested_relations}
    if requested_spec.get("kind") == "path" and requested_relations == {"is_a"}:
        effective_lens = requested_lens
        tree = project_hierarchy(concept_ids, lens=effective_lens)
    elif not lens_edges:
        effective_lens = "is_a"
        tree = project_hierarchy(concept_ids, lens=effective_lens)
    else:
        effective_lens = requested_lens
        tree = project_lens(concept_ids, lens_edges, requested_spec, touch=touch)

    reasons = set(core["reasons"])
    complete = not reasons
    authoritative = core["source_authoritative"] and complete
    completeness = {
        "complete": complete,
        # Use the explicit TRUNCATION_CAP_REASONS set, NOT an endswith("_cap") heuristic: `rename_hop_cap`
        # also ends in "_cap" but is a corruption-adjacent UNRESOLVED-IDENTITY signal (see the set's
        # docstring), so the heuristic would mislabel a >16-hop rename chain as a benign size truncation.
        "truncated": bool(reasons & TRUNCATION_CAP_REASONS),
        "reasons": sorted(reasons),
        "limits": {
            "membership_nodes": MAX_NODE_MEMBERSHIPS,
            "concepts_per_node": MAX_CONCEPTS_PER_NODE,
            "memberships": MAX_MEMBERSHIPS,
            "tree_nodes": MAX_TREE_NODES,
            "edges": MAX_EDGES,
            "edge_endpoints": MAX_EDGE_ENDPOINTS,
        },
        "source": {"membership_nodes": core["source_membership_nodes"],
                   "edges": core["source_edges"]},
        "included": {
            "membership_nodes": core["included_membership_nodes"],
            "memberships": core["membership_count"],
            "concepts": len(concept_ids),
            "tree_nodes": len(tree.get("nodes") or {}),
            "edges": len(edges),
            "derived_edges": core["derived_edges"],
            "experiment_refs": core["reference_count"],
        },
        "source_integrity": core["source_integrity"],
    }
    return {
        "schema": CONCEPT_FRAME_SCHEMA,
        "status": "complete" if complete else "partial",
        "run_id": core["run_id"],
        RUN_GENERATION_FIELD: core[RUN_GENERATION_FIELD],
        "requested_seq": core["requested_seq"],
        "captured_seq": core["captured_seq"],
        "max_seq": core["max_seq"],
        "historical": core["captured_seq"] < core["max_seq"],
        "requested_lens": requested_lens,
        "effective_lens": effective_lens,
        "lens": effective_lens,
        "requested_lens_spec": {
            "name": requested_lens,
            "rels": list(requested_spec.get("rels") or []),
            "kind": str(requested_spec.get("kind") or "edge"),
            "registration": lens_registration,
        },
        "lens_contract": {
            "requested": requested_lens,
            "effective": effective_lens,
            "registration": lens_registration,
            "fallback": (None if requested_lens == effective_lens else "no_matching_edges"),
        },
        "lenses": lens_pack,
        "tree": tree,
        "metrics": core["metrics"],
        "touch": touch,
        "edges_present": bool(edges),
        "lens_edges_present": (False if requested_spec.get("kind") == "path" else bool(lens_edges)),
        "experiment_refs": core["experiment_refs"],
        "authoritative": authoritative,
        "authority": {
            "authoritative": authoritative,
            "source_authoritative": core["source_authoritative"],
            "complete": complete,
            "scope": "captured_recoverable_event_prefix",
            # Membership is authoritative AS A RECORDED CLAIM, not proof of taxonomy truth.
            "semantic_claims_verified": False,
        },
        "provenance": {
            "source": "events.jsonl",
            "projection": "event_log_fold",
            "membership_semantics": "recorded_claims",
            "membership_counts": core["provenance_counts"],
        },
        "complete": complete,
        "completeness": completeness,
    }


def build_frame(state, *, run_id: str, requested_lens: str, lens_pack: list[dict],
                generation: Optional[str], requested_seq: Optional[int], captured_seq: int,
                max_seq: int, source_divergence: Optional[dict],
                requested_spec: Optional[dict] = None,
                lens_registration: str = "shipped") -> dict:
    """Compatibility wrapper: build one core, then apply one pure lens projection."""
    core = build_core(
        state, run_id=run_id, lens_pack=lens_pack, generation=generation,
        requested_seq=requested_seq, captured_seq=captured_seq, max_seq=max_seq,
        source_divergence=source_divergence)
    return project_frame(
        core, requested_lens=requested_lens, lens_pack=lens_pack,
        requested_spec=requested_spec, lens_registration=lens_registration)
