"""The fold's CONCEPT family: every handler that writes the run's Part IV/V concept read model.

Split out of `events/replay.py` (review 2026-09-22, EVT-12), where these ~720 lines changed for
their own reasons — the concept taxonomy's rules — beside a node-lifecycle fold that changes for
others. What lives here:

* the node MEMBERSHIP sidecars and their three producers — the proposer's authoring envelope
  (`_fold_node_concept_envelope`, called by `replay.py::_on_node_created`), the classifier cadence
  (`_on_node_concepts`) and the operator edit (`_on_concept_tag_edited`) — which all publish through
  the ONE write `_publish_node_membership`;
* the run BASE (`_on_run_concepts`) and the delta post-pass that materializes authored deltas over
  the whole DAG (`_materialize_concept_deltas`, called by `replay.py::_finalize_fold`);
* the coverage snapshots, hypothesis concepts, consolidation renames and typed concept edges.

Advisory by construction, as every handler's own comment says: none of it re-ranks an evaluated node
or moves the champion; it steers later proposals. Moved VERBATIM, comments included; `replay.py`
re-exports the names tests import or read, and merges `HANDLERS` into its dispatch table.
"""
from __future__ import annotations

import heapq
import math

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
from looplab.core.models import (INHERITABLE_CONCEPT_PROVENANCE as _INHERITABLE_CONCEPT_PROVENANCE,
                                 NODE_CONCEPT_PROVENANCE_AUTHORED,
                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                                 NODE_CONCEPT_PROVENANCE_OPERATOR,
                                 NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                                 Event, Node, RunState,
                                 coerce_node_id as _coerce_node_id,
                                 node_concept_event_provenance)
from looplab.events.replay_ctx import _MISSING, _FoldCtx, _event_generation
from looplab.events.types import (EV_CONCEPT_CONSOLIDATION, EV_CONCEPT_COVERAGE_SNAPSHOT,
                                  EV_CONCEPT_EDGE, EV_CONCEPT_TAG_EDITED, EV_HYPOTHESIS_CONCEPTS,
                                  EV_NODE_CONCEPTS, EV_RUN_CONCEPTS)


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
# `directive` drive the pivot cue, `current_streak`/`current_axis` (else `recent_axis`/`locked_axis`,
# on a row recorded before `current_axis` existed — review 2026-09-22, SCJ-10) drive
# capability-expansion, and the rest is display/diagnostic. Anything else on the row is dropped.
_COVERAGE_SNAPSHOT_STR = ("projection_token", "directive", "top_concept", "locked_axis",
                          "recent_axis", "current_axis", "tag_mode")
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

def _publish_node_membership(st: RunState, nid: int, values, provenance: str) -> None:
    """The ONE write of a node's EFFECTIVE concept membership and the producer that owns it.

    Three handlers publish a membership — the authoring envelope, the classifier cadence and the
    operator edit — and every one of them REPLACES what was there. That is correct for the membership
    (the last valid writer wins is the whole read-model contract) and it is what silently destroyed
    the PROPOSER's own claim: the cadence handler assigned its bounded values straight onto the
    membership map and nothing else in the fold held them, so `RunState.node_concepts_authored` is
    written by the authoring envelope alone and this function may never touch it. Stating that here,
    at the one site every replacement goes through, is the point of the funnel: a fourth producer
    added later inherits the rule instead of re-deriving it.

    The two sidecars this DOES write are the pair no writer may set apart from the other — a
    membership whose provenance still names the previous producer is exactly the trust inversion
    `classifier_verified_node_concepts` fails closed on.
    """
    st.node_concepts[nid] = list(values)
    st.node_concept_provenance[nid] = provenance


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
        st.node_concepts_at_pending.pop(n.id, None)
        st.node_concept_deltas.pop(n.id, None)
        ctx.concept_subject_invalidated.add(n.id)
    # THE PROPOSER'S OWN CLAIM, RECORDED WHOEVER ENDS UP OWNING THE MEMBERSHIP (`node_concepts_authored`).
    # Decided HERE — before the membership branches and outside `receipt_protected` — because this
    # record follows the IDEA and never the membership: the concept envelope is excluded from the
    # subject-equality test above, so a protected re-emission may legitimately carry a NEW authored set
    # while the classifier keeps the membership, and a record derived inside the branches would freeze
    # the first authoring forever. Full sets only; a `delta` node's operands stay in
    # `node_concept_deltas` (which no classifier writer clears), and an unsupported mode authors
    # nothing this fold is willing to read.
    if unsupported_mode or delta_mode:
        st.node_concepts_authored.pop(n.id, None)
    elif n.idea.concepts or recognized_mode == "full":
        # An explicit `full` + [] is an authored KNOWN-EMPTY set, the same distinction the membership
        # branch below draws, and it is not the same statement as "this node authored nothing".
        st.node_concepts_authored[n.id] = [str(c) for c in n.idea.concepts]
    else:
        st.node_concepts_authored.pop(n.id, None)
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
        st.node_concepts_at_pending.pop(n.id, None)
    elif (not unsupported_mode and not receipt_protected
          and (n.idea.concepts or recognized_mode == "full")):
        # Full is an exact replacement. An explicit `full` + [] is therefore a known-empty membership,
        # while an old no-mode/no-concepts payload stays genuinely absent for replay compatibility.
        st.node_concept_deltas.pop(n.id, None)
        _publish_node_membership(st, n.id, [str(c) for c in n.idea.concepts],
                                 NODE_CONCEPT_PROVENANCE_AUTHORED)
        st.node_concepts_at_vocab.pop(n.id, None)
        st.node_concepts_at_pending.pop(n.id, None)
    elif not receipt_protected:
        # Unknown mode and genuinely absent legacy membership are both non-authoritative. A pending
        # replacement must not retain a previous authored set merely because classifier-protected
        # subject equality intentionally ignores the proposer concept envelope.
        st.node_concept_deltas.pop(n.id, None)
        if st.node_concept_provenance.get(n.id) == NODE_CONCEPT_PROVENANCE_AUTHORED:
            st.node_concepts.pop(n.id, None)
            st.node_concept_provenance.pop(n.id, None)
            st.node_concepts_at_vocab.pop(n.id, None)
            st.node_concepts_at_pending.pop(n.id, None)


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
    _publish_node_membership(st, nid, bounded, incoming_provenance)
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
    # F1i: how many nodes were still PENDING when this row was produced. It is READ ONLY as an evidence
    # gate (`core/models.py::classifier_verified_node_concepts`, `_graded_novelty_precheck`), never as a
    # display filter. Same fail-closed shape as `at_vocab` and the same reason: only the reviewed
    # classifier's own writer stamps it, so a future/malformed producer cannot claim quiescence — but the
    # DEFAULT here is the opposite direction, absent == 0 == quiescent, because that is what every log
    # written before this cadence could fire mid-eval provably was. A malformed value fails CLOSED to
    # "in flight" rather than to 0: a row that cannot say when it was made is not evidence.
    ap = d.get("at_pending")
    if ap is None:
        st.node_concepts_at_pending.pop(nid, None)
    elif isinstance(ap, int) and not isinstance(ap, bool) and ap >= 0:
        st.node_concepts_at_pending[nid] = ap
    else:
        st.node_concepts_at_pending[nid] = 1


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
    _publish_node_membership(st, nid, bounded, NODE_CONCEPT_PROVENANCE_OPERATOR)
    ctx.concept_mode_untrusted.discard(nid)
    ctx.concept_input_capped.discard(nid)
    ctx.concept_input_invalid.discard(nid)
    if overflow:
        ctx.concept_input_capped.add(nid)
    if invalid:
        ctx.concept_input_invalid.add(nid)
    # Operator tags are not vocabulary-versioned; clear any classifier staleness receipt for this node.
    st.node_concepts_at_vocab.pop(nid, None)
    # …and the F1i in-flight receipt: an operator assertion is a human's, not a mid-eval classifier's.
    st.node_concepts_at_pending.pop(nid, None)


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
        #     drops the same edge (`finite_metric` returns None -> rejected at serve/concept_frame.py::bounded_inputs),
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


# This family's rows of the fold's dispatch table. `replay.py::_HANDLERS` is assembled from every
# family's table and refuses a type two of them claim, so a concept handler is registered HERE,
# beside its body, and nowhere else.
HANDLERS = {
    EV_CONCEPT_COVERAGE_SNAPSHOT: _on_concept_coverage_snapshot,
    EV_NODE_CONCEPTS: _on_node_concepts,
    EV_RUN_CONCEPTS: _on_run_concepts,
    EV_CONCEPT_TAG_EDITED: _on_concept_tag_edited,
    EV_HYPOTHESIS_CONCEPTS: _on_hypothesis_concepts,
    EV_CONCEPT_CONSOLIDATION: _on_concept_consolidation,
    EV_CONCEPT_EDGE: _on_concept_edge,
}
