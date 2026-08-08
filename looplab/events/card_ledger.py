"""The derived Card ledger: fold-time receipt bounds + the ``derive_cards`` post-pass.

Extracted from ``events/replay.py`` (doc 25 EV-01), where ``_derive_cards`` had grown into an
~850-line function inside the fold module even though it never sees an Event: it is invoked exactly
once, from ``replay._finalize_fold``, as a pure post-pass over the already-folded ``RunState``.

Two halves live here, and the split between them is the one that matters:

* the fold-time bounds (``_bounded_card_*``, ``_card_replay_*``) shrink one untrusted event payload
  into the receipt ``replay``'s ``card_*`` handlers append to ``RunState``. They run per event.
* ``derive_cards(st)`` and its phase functions read only those receipts plus folded node state, and
  never an ``Event``. They run once, at the end.

Both are pure and deterministic (engine invariant 5): no I/O, no clock, no LLM, and order-tolerant
over independent events. This module imports only ``looplab.core`` — it must NOT import ``replay``,
which imports it.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Collection, Iterable, Mapping
# `dataclasses.field` is reached through the module on purpose: the merge fold copies the action
# block with `for field in (...)`, and a bare `field` import would be shadowed by that loop.
import dataclasses
from dataclasses import dataclass
from typing import Literal

from looplab.core.concepts import (
    CONCEPT_INVALID_ID_REASON,
    CONCEPTS_PER_NODE_CAP_REASON,
    ConceptMaterializationReason,
    bounded_raw_concept_values,
    concept_materialization_receipt,
    normalized_concept_materialization_receipt,
)
from looplab.core.jsonutil import valid_digest_ref
from looplab.core.models import (CARD_ACTION_DIGEST_V1_FIELDS, CARD_ACTION_DIGEST_V2_FIELDS,
                     INHERITABLE_CONCEPT_PROVENANCE as _INHERITABLE_CONCEPT_PROVENANCE,
                     NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                     Card, CardConceptSource, CardIdentityProvenance, CardSelectionProvenance,
                     Node, NodeStatus, RunState, card_action_digest,
                     card_ownership_receipt, coerce_node_id as _coerce_node_id,
                     is_unevaluated_speculative_discard,
                     legacy_card_ownership_receipt_v1,
                     transitional_card_action_digest_v1,
                     transitional_card_ownership_receipt_v1,
                     hypothesis_id, hypothesis_statement_digest,
                     idea_proposal_digest,
                     node_counts_toward_card_budget,
                     normalize_extra_metrics, normalize_researcher_footprint,
                     normalize_steering_context,
                     valid_card_action_digest, valid_researcher_footprint)

# A module-private "key absent" marker for ``dict.get``, so an absent receipt stays distinguishable
# from a stored ``None``. ``replay._MISSING`` is the same idea for the fold's handlers; neither
# sentinel ever crosses a module boundary, so these are two private markers rather than one rule
# spelled twice.
_MISSING = object()


_CARD_REPLAY_ID_MAX = 256
_CARD_REPLAY_STATEMENT_MAX = 4_000
_CARD_REPLAY_SOURCE_MAX = 64
_CARD_REPLAY_RATIONALE_MAX = 400
_CARD_REPLAY_ACTION_MAP_MAX = 64
_CARD_REPLAY_ACTION_LIST_MAX = 64
_CARD_REPLAY_MERGE_ALIASES_MAX = 256
_CARD_REPLAY_NODE_ID_MAX = (1 << 31) - 1


def _card_replay_id(value) -> str | None:
    """Return one canonical card id without copying an oversized hostile string."""
    if not isinstance(value, str) or len(value) > _CARD_REPLAY_ID_MAX:
        return None
    bounded = value.strip()
    return bounded if bounded and bounded.isprintable() else None


def _card_replay_text(
    value, *, max_chars: int, strip: bool = False, allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str) or len(value) > max_chars:
        return None
    bounded = value.strip() if strip else value
    return bounded if bounded or allow_empty else None


def _card_replay_node_id(value) -> int | None:
    node_id = _coerce_node_id({"node_id": value})
    return node_id if node_id is not None and 0 <= node_id <= _CARD_REPLAY_NODE_ID_MAX else None


def _bounded_card_action_map(value) -> dict[str, float]:
    """Normalize a scalar map with lexical top-K identity and O(K) temporary memory."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        if (not isinstance(raw_key, str) or not raw_key or len(raw_key) > 200
                or isinstance(raw_value, bool) or not isinstance(raw_value, (int, float))):
            continue
        try:
            number = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(number):
            continue
        if len(out) >= _CARD_REPLAY_ACTION_MAP_MAX:
            greatest = max(out)
            if raw_key >= greatest:
                continue
            del out[greatest]
        out[raw_key] = number
    return dict(sorted(out.items()))


def _bounded_card_action_space(value) -> dict[str, list[float]]:
    """Normalize a search space without sorting/copying an attacker-sized mapping."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[float]] = {}
    rows = heapq.nsmallest(
        _CARD_REPLAY_ACTION_MAP_MAX,
        ((key, raw) for key, raw in value.items()
         if isinstance(key, str) and key and len(key) <= 200),
        key=lambda row: row[0],
    )
    for raw_key, raw_values in rows:
        if not isinstance(raw_values, list):
            continue
        values: list[float] = []
        for raw_value in raw_values[:_CARD_REPLAY_ACTION_LIST_MAX]:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                continue
            try:
                number = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                values.append(number)
        out[raw_key] = values
    return out


def _bounded_card_eval_timeout(value) -> tuple[float | None, bool]:
    """Decode a PRESENT `eval_timeout` into `(timeout, valid)`.

    `(None, True)` is an explicit null — a card that deliberately clears the timeout — and
    `(None, False)` is an unusable value. Callers guard on the key being present, so "absent" never
    reaches here and the two Nones cannot be confused.

    Shared by the fold's admission bound and the derive-time snapshot (doc 25 EV-02), which had
    byte-similar copies of this ladder. The bool guard is the part worth spelling once:
    `isinstance(True, int)` is True in Python, so a payload carrying `eval_timeout: true` would
    otherwise become a 1-second timeout.
    """
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    return (timeout, True) if math.isfinite(timeout) and timeout > 0 else (None, False)


def _bounded_card_parent_ids(value) -> list[int]:
    """Bounded, de-duplicated, first-wins node ids from an untrusted parent list.

    The third copy of this loop (doc 25 EV-02): both admission sites and the snapshot walked it
    identically. Order is preserved rather than sorted, because a card's parent order is part of what
    the action digest covers.
    """
    out: list[int] = []
    if not isinstance(value, list):
        return out
    for raw in value[:_CARD_REPLAY_ACTION_LIST_MAX]:
        parent_id = _card_replay_node_id(raw)
        if parent_id is not None and parent_id not in out:
            out.append(parent_id)
    return out


def _bounded_card_action(value: dict, *, record_unknown_fields: bool = False) -> dict:
    """Copy only the action fields consumed by ``_card_added_snapshot``."""
    out: dict = {}
    operator = _card_replay_text(value.get("operator"), max_chars=64, strip=True)
    if operator is not None:
        out["operator"] = operator
    if isinstance(value.get("params"), dict):
        out["params"] = _bounded_card_action_map(value["params"])
    if isinstance(value.get("space"), dict):
        out["space"] = _bounded_card_action_space(value["space"])
    profile = _card_replay_text(value.get("eval_profile"), max_chars=256, allow_empty=True)
    if profile is not None:
        out["eval_profile"] = profile
    if "eval_timeout" in value:
        timeout, timeout_valid = _bounded_card_eval_timeout(value.get("eval_timeout"))
        if timeout_valid:
            out["eval_timeout"] = timeout           # None = an explicit "no timeout"
        else:
            out["_eval_timeout_invalid"] = True

    concept_key = "concept_tags" if "concept_tags" in value else "concepts" if "concepts" in value else None
    if concept_key is not None:
        raw_concepts = value.get(concept_key)
        concepts, overflow, invalid = bounded_raw_concept_values(raw_concepts)
        if isinstance(raw_concepts, list):
            out[concept_key] = concepts
        # These flags are produced here, never trusted from the event. They retain the truth that a
        # compact membership is only a projection rather than forging a complete proposal receipt.
        out["_concept_tags_overflow"] = overflow
        out["_concept_tags_invalid"] = invalid

    if isinstance(value.get("parent_ids"), list):
        out["parent_ids"] = _bounded_card_parent_ids(value["parent_ids"])
    parent_id = _card_replay_node_id(value.get("parent_id"))
    if parent_id is not None:
        out["parent_id"] = parent_id
    if record_unknown_fields:
        known_fields = {
            "operator", "params", "space", "eval_profile", "eval_timeout",
            "concept_tags", "concepts",
            "parent_id", "parent_ids",
        }
        if any(field not in known_fields for field in value):
            # retain only the fact that executable meaning was discarded. Copying an
            # unknown value would defeat the replay bound; forgetting its existence could turn a
            # lossy future-schema action into a receipt-backed selectable Card.
            out["_unknown_action_fields"] = True
    return out


def _bounded_card_ownership_receipt(value, *, card_id: str | None) -> dict | None:
    """Retain one exact, constant-size supported ownership proof and reject every extension."""
    keys = {"v", "card_id", "action_digest"}
    if not isinstance(value, dict) or set(value) != keys or card_id is None:
        return None
    digest = value.get("action_digest")
    version = value.get("v")
    if (type(version) is not int or version not in {1, 2}
            or value.get("card_id") != card_id
            or not valid_card_action_digest(digest, version=version)):
        return None
    return {"v": version, "card_id": card_id, "action_digest": digest}


def _bounded_card_added_receipt(d: dict) -> dict | None:
    """Canonical replay input for a ``card_added`` envelope."""
    rec: dict = {}
    card_id = _card_replay_id(d.get("id"))
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if card_id is None and statement is None:
        return None
    if card_id is not None:
        rec["id"] = card_id
    if statement is not None:
        rec["statement"] = statement
    source = _card_replay_text(d.get("source"), max_chars=_CARD_REPLAY_SOURCE_MAX, strip=True)
    if source is not None:
        rec["source"] = source
    rationale = _card_replay_text(d.get("rationale"), max_chars=_CARD_REPLAY_RATIONALE_MAX)
    if rationale is not None:
        rec["rationale"] = rationale
    at_node = _card_replay_node_id(d.get("at_node"))
    if at_node is not None:
        rec["at_node"] = at_node

    if isinstance(d.get("idea"), dict):
        # An explicit (even empty) idea owns the snapshot in historical replay; retaining that shape keeps
        # a top-level fallback action from silently overriding it after sanitization.
        rec["idea"] = _bounded_card_action(d["idea"], record_unknown_fields=True)
    else:
        rec.update(_bounded_card_action(d))

    if isinstance(d.get("parent_ids"), list):
        parent_ids: list[int] = []
        for raw_parent in d["parent_ids"][:_CARD_REPLAY_ACTION_LIST_MAX]:
            parent_id = _card_replay_node_id(raw_parent)
            if parent_id is not None and parent_id not in parent_ids:
                parent_ids.append(parent_id)
        rec["parent_ids"] = parent_ids
    parent_id = _card_replay_node_id(d.get("parent_id"))
    if parent_id is not None:
        rec["parent_id"] = parent_id
    scored_against = _card_replay_node_id(d.get("scored_against"))
    if scored_against is not None:
        rec["scored_against"] = scored_against
    if "parent_generations" in d:
        raw_generations = d.get("parent_generations")
        generations: dict[str, int] = {}
        valid_generations = (
            isinstance(raw_generations, dict)
            and len(raw_generations) <= _CARD_REPLAY_ACTION_LIST_MAX
        )
        if valid_generations:
            for raw_parent, raw_generation in raw_generations.items():
                if not isinstance(raw_parent, str) or len(raw_parent) > 10:
                    valid_generations = False
                    break
                parent = _card_replay_node_id(raw_parent)
                if (parent is None or raw_parent != str(parent)
                        or type(raw_generation) is not int
                        or not 0 <= raw_generation <= _CARD_REPLAY_NODE_ID_MAX):
                    valid_generations = False
                    break
                generations[raw_parent] = raw_generation
        if valid_generations:
            rec["parent_generations"] = dict(sorted(generations.items()))
        else:
            rec["_parent_generations_invalid"] = True
    if "scored_against_generation" in d:
        raw_generation = d.get("scored_against_generation")
        if raw_generation is None:
            rec["scored_against_generation"] = None
        elif (type(raw_generation) is int
              and 0 <= raw_generation <= _CARD_REPLAY_NODE_ID_MAX):
            rec["scored_against_generation"] = raw_generation
        else:
            rec["_scored_against_generation_invalid"] = True
    if "scored_against_empty" in d:
        if type(d.get("scored_against_empty")) is bool:
            rec["scored_against_empty"] = d["scored_against_empty"]
        else:
            rec["_scored_against_empty_invalid"] = True
    raw_footprint = d.get("footprint")
    footprint = normalize_researcher_footprint(raw_footprint)
    if footprint is not None:
        rec["footprint"] = footprint
    if (raw_footprint is not None
            and (not isinstance(raw_footprint, dict) or len(raw_footprint) > 2
                 or not valid_researcher_footprint(raw_footprint))):
        rec["_footprint_invalid"] = True
    if "steering_context" in d:
        steering = normalize_steering_context(d.get("steering_context"))
        if steering is not None:
            rec["steering_context"] = steering
        else:
            rec["_steering_context_invalid"] = True
    ownership_receipt = _bounded_card_ownership_receipt(
        d.get("ownership_receipt"), card_id=card_id)
    if ownership_receipt is not None:
        rec["ownership_receipt"] = ownership_receipt
    return rec


def _bounded_card_merge_receipt(d: dict) -> dict | None:
    canonical = _card_replay_id(d.get("canonical"))
    raw_aliases = d.get("aliases")
    if canonical is None or not isinstance(raw_aliases, list) or not raw_aliases:
        return None
    aliases: list[str] = []
    for raw_alias in raw_aliases[:_CARD_REPLAY_MERGE_ALIASES_MAX]:
        alias = _card_replay_id(raw_alias)
        if alias is not None and alias not in aliases:
            aliases.append(alias)
    if not aliases:
        return None
    rec = {"canonical": canonical, "aliases": aliases}
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if statement is not None:
        rec["statement"] = statement
    return rec


def _bounded_card_drop_receipt(d: dict) -> dict | None:
    card_id = _card_replay_id(d.get("id"))
    if card_id is None:
        return None
    rec = {"id": card_id}
    reason = _card_replay_text(d.get("reason"), max_chars=_CARD_REPLAY_RATIONALE_MAX)
    if reason is not None:
        rec["reason"] = reason
    raw_dropped_by = d.get("dropped_by")
    if raw_dropped_by is None or raw_dropped_by == "":
        raw_dropped_by = d.get("by")
    dropped_by = _card_replay_text(
        raw_dropped_by, max_chars=_CARD_REPLAY_SOURCE_MAX, strip=True)
    if dropped_by is not None:
        rec["dropped_by"] = dropped_by
    return rec


def _record_setter_ids(nodes: dict[int, Node], direction: str) -> set[int]:
    """The run-global set of node ids that ADVANCED the run's SOTA — sticky evidence.

    Pure helper (Layer 1a) extracted VERBATIM from `_derive_hypotheses` so `_derive_cards` reuses the
    identical logic. A node counts if it is evaluated/feasible/non-tombstoned and, in creation order,
    either ESTABLISHES the first SOTA or BEATS the standing record; the flag STAYS set even after a
    later node overtakes it (so a draft-backed hypothesis/card does not flip supported->tested the
    moment something beats it — computing "is the CURRENT best" made it a board bug). Never mutates
    `nodes`."""
    better = (lambda a, b: a > b) if direction == "max" else (lambda a, b: a < b)
    setters: set[int] = set()
    running: float | None = None
    for n in sorted(nodes.values(), key=lambda x: x.id):
        if (n.status is NodeStatus.evaluated and n.feasible and n.metric is not None
                and not n.tombstoned):              # §6.3: a deleted node must not set the board's SOTA
            if running is None or better(n.metric, running):
                setters.add(n.id)                   # first node ESTABLISHES the SOTA, or a later node
                running = n.metric                  # BEATS the standing record — either is a real advance
    return setters


def _evidence_verdict(evidence_ids: Iterable[int], nodes: dict[int, Node], direction: str,
                      record_setters: set[int], is_abandoned: bool,
                      ) -> tuple[float | None, str, bool]:
    """Compute (best_delta, status, supported) for one hypothesis/card from its evidence nodes.

    Pure, VALUES-returning helper (Layer 1a) extracted VERBATIM from `_derive_hypotheses` so a card's
    verdict is byte-identical to the hash-joined hypothesis wherever their evidence sets coincide. NEVER
    stamps onto Node/Hypothesis/Card — it only reads. `record_setters` is `_record_setter_ids(...)`;
    `is_abandoned` is the caller's "id in <abandoned set>" check. Supported if an experiment IMPROVED
    over its parent (or set a run record); tested if evaluated without improvement; testing while
    evidence still runs; open with no (usable) evidence; abandoned overrides all."""
    better = (lambda a, b: a > b) if direction == "max" else (lambda a, b: a < b)
    ev = [nodes[i] for i in evidence_ids if i in nodes and not nodes[i].tombstoned]
    evaluated = [n for n in ev if n.status is NodeStatus.evaluated and n.feasible
                 and n.metric is not None]
    supported = False
    best_delta: float | None = None
    for n in evaluated:
        # parent metric = the best feasible-evaluated parent's metric (direction-aware)
        pmetrics = [nodes[p].metric for p in n.parent_ids
                    if p in nodes and nodes[p].metric is not None
                    and nodes[p].feasible]
        base = (max(pmetrics) if direction == "max" else min(pmetrics)) if pmetrics else None
        if base is not None:
            delta = (n.metric - base) if direction == "max" else (base - n.metric)
            best_delta = delta if best_delta is None else max(best_delta, delta)
            if better(n.metric, base):
                supported = True
        if n.id in record_setters:                 # a draft/node that advanced the run's SOTA (sticky —
            supported = True                       # stays supported even after a later node overtakes it)
    pending = [n for n in ev if n.status is NodeStatus.pending]
    if is_abandoned:
        status = "abandoned"
    elif not ev:
        status = "open"
    elif supported:
        status = "supported"                       # at least one experiment improved — verdict stands
    elif pending:
        status = "testing"                         # still inconclusive: evidence running
    elif not evaluated:
        status = "open"                            # all evidence failed/infeasible — no verdict
    else:
        status = "tested"                          # all evidence evaluated, none improved
    return best_delta, status, supported


def _bounded_card_enrichment(value, *, depth: int = 0, budget: list[int] | None = None):
    """Return a bounded JSON-shaped enrichment value, or ``(False, None)`` when unusable."""
    if budget is None:
        budget = [256]
    if budget[0] <= 0 or depth > 4:
        return False, None
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return (True, value) if abs(value) <= (1 << 53) - 1 else (False, None)
    if isinstance(value, float):
        return (True, value) if math.isfinite(value) else (False, None)
    if isinstance(value, str):
        return True, value[:400]
    if isinstance(value, list):
        out = []
        for item in value[:64]:
            valid, bounded = _bounded_card_enrichment(item, depth=depth + 1, budget=budget)
            if valid:
                out.append(bounded)
        return True, out
    if isinstance(value, dict):
        out = {}
        # sorting the whole hostile map is an O(n) temporary-memory amplification before
        # the 64-row output cap. A lexical heap keeps deterministic top-K semantics in O(K) memory.
        rows = heapq.nsmallest(
            64,
            ((key, item) for key, item in value.items()
             if isinstance(key, str) and key and len(key) <= 128),
            key=lambda row: row[0],
        )
        for key, item in rows:
            valid, bounded = _bounded_card_enrichment(item, depth=depth + 1, budget=budget)
            if valid:
                out[key] = bounded
        return True, out
    return False, None


def _bounded_card_ref(value) -> str | None:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 400 or not value.isprintable()):
        return None
    return value


def _digest_ref(value: str, namespace: str) -> bool:
    # Delegating also FIXES this copy (doc 25 EV-04): it lacked the `isinstance` guard its siblings
    # had, so a non-string reached `.startswith` and raised `AttributeError` inside the fold — which
    # takes down every replay of the run, not just this field. Both call sites happen to pass an
    # already-bounded `str` today, so it was latent rather than live.
    return valid_digest_ref(value, prefix=f"{namespace}:sha256:")


def _bounded_card_footprint_enrichment(value) -> dict | None:
    if (not isinstance(value, dict) or not value
            or not set(value) <= {"gpus", "gpu_mem_mib", "proposed_by", "finalized_by"}):
        return None
    quantitative = normalize_researcher_footprint(value) or {}
    if "gpus" in value and "gpus" not in quantitative:
        return None
    if "gpu_mem_mib" in value and "gpu_mem_mib" not in quantitative:
        return None
    if "proposed_by" in value:
        if value["proposed_by"] != "researcher":
            return None
        quantitative["proposed_by"] = "researcher"
    if "finalized_by" in value:
        if value["finalized_by"] != "developer":
            return None
        quantitative["finalized_by"] = "developer"
    return quantitative or None


def _bounded_card_novelty_enrichment(value) -> dict | None:
    if (not isinstance(value, dict)
            or not set(value) <= {"grade", "level", "near_node", "near_generation", "recommendation"}):
        return None
    out: dict = {}
    for key in ("grade", "recommendation"):
        raw = value.get(key)
        if raw is not None:
            if not isinstance(raw, str) or len(raw) > 200 or not raw.isprintable():
                return None
            out[key] = raw
    for key, maximum in (("level", 16), ("near_node", (1 << 31) - 1),
                         ("near_generation", (1 << 31) - 1)):
        if key in value:
            raw = value[key]
            if type(raw) is not int or not 0 <= raw <= maximum:
                return None
            out[key] = raw
    return out


_CARD_CROSS_RUN_ROOT_KEYS = {
    "v", "matched_concepts", "prior_runs", "prior_runs_total", "prior_runs_omitted",
    "prior_runs_complete", "concept_source",
}
_CARD_CROSS_RUN_ROW_KEYS = {
    "run", "run_id", "metric", "best_metric", "run_best_metric", "similarity", "concepts",
    "matched_concepts", "outcomes", "matched_concept_outcomes", "source_receipt",
}
_CARD_CROSS_RUN_SOURCE_KEYS = {
    "source_complete", "partial_capsules", "source_unknown_capsules", "source_concepts_omitted",
    "source_outcomes_omitted", "source_store_complete", "source_rows_total",
    "source_rows_quarantined", "source_malformed_rows", "source_invalid_capsule_rows",
    "source_duplicate_run_rows",
}


def _bounded_card_cross_run_enrichment(value) -> dict | None:
    if not isinstance(value, dict) or not set(value) <= _CARD_CROSS_RUN_ROOT_KEYS:
        return None
    runs = value.get("prior_runs", [])
    if (not isinstance(runs, list) or len(runs) > 64
            or any(not isinstance(row, dict) or not set(row) <= _CARD_CROSS_RUN_ROW_KEYS
                   for row in runs)):
        return None
    source = value.get("concept_source", {})
    if (not isinstance(source, dict) or not set(source) <= _CARD_CROSS_RUN_SOURCE_KEYS):
        return None
    # This existing projection already normalizes counts, list caps and source completeness. The closed
    # key checks above are what prevent an arbitrary body/path from entering the Card read model.
    return _card_cross_run_projection(value)


def _proposal_card_concept_source(
    kind: Literal["card_added", "card_enriched"], *, present: bool,
    overflow: bool = False, invalid: bool = False,
) -> CardConceptSource:
    reasons: set[ConceptMaterializationReason] = set()
    if overflow:
        reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
    if invalid:
        reasons.add(CONCEPT_INVALID_ID_REASON)
    receipt = concept_materialization_receipt(reasons)
    return CardConceptSource(
        kind=kind,
        membership_present=present,
        complete=present and receipt is None,
        materialization_receipt=receipt,
    )


def _card_added_snapshot(d: dict) -> tuple[dict, bool]:
    """Tolerantly decode one bounded, node-less card action snapshot."""
    idea = d.get("idea") if isinstance(d.get("idea"), dict) else d
    snapshot: dict = {}
    owns_action = False
    operator = idea.get("operator")
    if isinstance(operator, str) and operator.strip() and len(operator.strip()) <= 64:
        snapshot["operator"] = operator.strip()
        owns_action = True
    if isinstance(idea.get("params"), dict):
        snapshot["params"] = normalize_extra_metrics(idea["params"], max_items=64)
        owns_action = True
    if isinstance(idea.get("space"), dict):
        # The SAME bound the fold's admission applies (doc 25 EV-02). The copy here sliced the top-64
        # BEFORE filtering keys, so an unusable key that sorts early consumed the window and the two
        # stages disagreed about the same input: 64 usable keys admitted, 14 decoded. Not reachable
        # today — only already-bounded `st.cards_added` rows reach this function — but nothing
        # enforced that, and the drift was silent by construction.
        snapshot["space"] = _bounded_card_action_space(idea["space"])
        owns_action = True
    profile = idea.get("eval_profile")
    if isinstance(profile, str) and len(profile) <= 256:
        snapshot["eval_profile"] = profile
        owns_action = True
    if "eval_timeout" in idea:
        timeout, timeout_valid = _bounded_card_eval_timeout(idea.get("eval_timeout"))
        if timeout_valid:
            snapshot["eval_timeout"] = timeout      # None = an explicit "no timeout"
            # An explicit null clears a timeout rather than declaring one, so it does not by itself
            # make this row an action owner — the pre-existing asymmetry, kept deliberately.
            owns_action = owns_action or timeout is not None
    concept_key_present = (
        "concept_tags" in idea or "concepts" in idea
        or "_concept_tags_overflow" in idea or "_concept_tags_invalid" in idea
    )
    raw_concepts = idea.get("concept_tags", idea.get("concepts"))
    if concept_key_present:
        values, overflow, invalid = bounded_raw_concept_values(raw_concepts)
        # Sanitized receipts carry these internal flags because the compact list alone cannot prove
        # whether the original membership was complete. Payload-provided flag fields are not copied.
        overflow = overflow or idea.get("_concept_tags_overflow") is True
        invalid = invalid or idea.get("_concept_tags_invalid") is True
        snapshot["concept_source"] = _proposal_card_concept_source(
            "card_added", present=isinstance(raw_concepts, list), overflow=overflow, invalid=invalid)
    else:
        snapshot["concept_source"] = _proposal_card_concept_source(
            "card_added", present=False)
    if isinstance(raw_concepts, list):
        snapshot["concept_tags"] = values
        owns_action = True

    raw_parent_ids = d.get("parent_ids", idea.get("parent_ids"))
    if isinstance(raw_parent_ids, list):
        snapshot["parent_ids"] = _bounded_card_parent_ids(raw_parent_ids)
        owns_action = True
    raw_parent_id = d.get("parent_id", idea.get("parent_id"))
    parent_id = _coerce_node_id({"node_id": raw_parent_id})
    if parent_id is not None and 0 <= parent_id <= (1 << 31) - 1:
        snapshot["parent_id"] = parent_id
        owns_action = True
    elif snapshot.get("parent_ids"):
        snapshot["parent_id"] = snapshot["parent_ids"][0]

    if isinstance(d.get("parent_generations"), dict):
        snapshot["parent_generations"] = dict(d["parent_generations"])

    scored_against = _coerce_node_id({"node_id": d.get("scored_against")})
    if scored_against is not None and 0 <= scored_against <= (1 << 31) - 1:
        snapshot["scored_against"] = scored_against
    if d.get("scored_against_generation") is None:
        if "scored_against_generation" in d:
            snapshot["scored_against_generation"] = None
    elif type(d.get("scored_against_generation")) is int:
        snapshot["scored_against_generation"] = d["scored_against_generation"]
    if type(d.get("scored_against_empty")) is bool:
        snapshot["scored_against_empty"] = d["scored_against_empty"]
    if isinstance(d.get("footprint"), dict):
        footprint = normalize_researcher_footprint(d["footprint"])
        if footprint is not None:
            snapshot["footprint"] = footprint
    if "steering_context" in d:
        steering = normalize_steering_context(d.get("steering_context"))
        if steering is not None:
            snapshot["steering_context"] = steering
    return snapshot, owns_action


_CARD_ADDED_ACTION_FIELDS = frozenset({
    "operator", "params", "space", "eval_profile", "eval_timeout", "concept_tags", "concepts",
    "parent_id", "parent_ids", "_concept_tags_overflow", "_concept_tags_invalid",
})


def _card_action_receipt_payload(snapshot: dict, *, version: int) -> dict:
    """Extract exactly the immutable action subset covered by one receipt version."""
    # Preserve absence: each digest owns canonical defaults for sparse historical receipts (notably
    # ``scored_against_empty=False`` and ``parent_ids=[]``). Materialising every missing member as None
    # changes those semantics and incorrectly demotes a structurally-valid but incomplete native receipt
    # to a synthesized shadow. Completeness is checked independently below and still fails closed.
    fields = CARD_ACTION_DIGEST_V1_FIELDS if version == 1 else CARD_ACTION_DIGEST_V2_FIELDS
    return {
        field: snapshot[field]
        for field in fields
        if field in snapshot
    }


def _card_added_ownership(
    d: dict, card_id: str, statement: str, snapshot: dict, *, owns_action: bool,
) -> tuple[bool, bool, str | None]:
    """Validate a native identity receipt and whether its action was losslessly represented."""
    explicit_id = d.get("id")
    receipt = d.get("ownership_receipt")
    receipt_version = receipt.get("v") if isinstance(receipt, dict) else None
    receipt_variant = None
    expected = None
    if receipt_version == 1:
        legacy_expected = legacy_card_ownership_receipt_v1(
            card_id, statement, _card_action_receipt_payload(snapshot, version=1))
        transitional_expected = transitional_card_ownership_receipt_v1(
            card_id, statement, _card_action_receipt_payload(snapshot, version=2))
        if receipt == legacy_expected:
            expected, receipt_variant = legacy_expected, "legacy-v1"
        elif receipt == transitional_expected:
            expected, receipt_variant = transitional_expected, "expanded-v1"
    elif receipt_version == 2:
        expected = card_ownership_receipt(
            card_id, statement, _card_action_receipt_payload(snapshot, version=2))
        if receipt == expected:
            receipt_variant = "v2"
    receipt_valid = bool(
        isinstance(explicit_id, str)
        and explicit_id == card_id
        and expected is not None
        and receipt == expected
    )
    raw_idea = d.get("idea")
    if not receipt_valid or not owns_action or not isinstance(raw_idea, dict):
        return receipt_valid, False, expected["action_digest"] if expected else None

    # A valid legacy v1 proof establishes durable native identity, but it predates timeout and lifecycle
    # fences. Keep it visible after upgrade while failing closed for execution/freshness decisions.
    if receipt_variant == "legacy-v1":
        return True, False, expected["action_digest"]

    # the receipt covers the complete executable subset. Unknown Idea members may gain
    # execution meaning in a later schema, so an old reader cannot silently discard them and still call
    # the action complete. Concept membership is the sole exception: it is metadata with its own receipt.
    if not set(raw_idea) <= _CARD_ADDED_ACTION_FIELDS:
        return True, False, expected["action_digest"]
    if ("eval_timeout" not in raw_idea
            or not {"parent_generations", "scored_against_generation",
                    "scored_against_empty"} <= set(d)
            or any(d.get(flag) is True for flag in {
                "_parent_generations_invalid", "_scored_against_generation_invalid",
                "_scored_against_empty_invalid", "_footprint_invalid",
            })):
        return True, False, expected["action_digest"]
    if ((d.get("scored_against") is None) != (d.get("scored_against_empty") is True)
            or (d.get("scored_against") is not None
                and type(d.get("scored_against_generation")) is not int)):
        return True, False, expected["action_digest"]
    if d.get("footprint") is not None and not valid_researcher_footprint(d.get("footprint")):
        return True, False, expected["action_digest"]
    raw_action = {
        "operator": raw_idea.get("operator"),
        "params": raw_idea.get("params"),
        "space": raw_idea.get("space"),
        "eval_profile": raw_idea.get("eval_profile"),
        "eval_timeout": raw_idea.get("eval_timeout"),
        "parent_id": d.get("parent_id", raw_idea.get("parent_id")),
        "parent_ids": d.get("parent_ids", raw_idea.get("parent_ids", [])),
        "parent_generations": d.get("parent_generations"),
        "scored_against": d.get("scored_against"),
        "scored_against_generation": d.get("scored_against_generation"),
        "scored_against_empty": d.get("scored_against_empty"),
        "footprint": d.get("footprint"),
    }
    raw_expected = (
        transitional_card_ownership_receipt_v1(card_id, statement, raw_action)
        if receipt_variant == "expanded-v1"
        else card_ownership_receipt(card_id, statement, raw_action)
    )
    action_complete = raw_expected == expected
    return True, action_complete, expected["action_digest"]


def _card_action_from_projection(card: Card) -> dict:
    return {
        "operator": card.operator,
        "params": card.params,
        "space": card.space,
        "eval_profile": card.eval_profile,
        "eval_timeout": card.eval_timeout,
        "parent_id": card.parent_id,
        "parent_ids": card.parent_ids,
        "parent_generations": card.parent_generations,
        "scored_against": card.scored_against,
        "scored_against_generation": card.scored_against_generation,
        "scored_against_empty": card.scored_against_empty,
        "footprint": card.footprint,
    }


# `is_unevaluated_speculative_discard` is still imported above — `tests/test_card_budget_refund.py`
# asserts one object under every name it is reachable by — but the fold no longer CALLS it directly.
# `node_counts_toward_card_budget` subsumes it, and that is deliberate: a replay-side binding the
# fold dispatches through is precisely a place for these two views to drift apart again.
def _card_debug_leaf_children(st: RunState) -> dict[int, frozenset[int]]:
    """Child node ids that END a failed node's life as a debuggable leaf, keyed by parent id.

    A node with a child is no longer a leaf — EXCEPT for a child the Card lane's policy cannot see.
    That universe is not a judgement call this module gets to make: ``card_selection``'s
    ``_effective_policy_state`` builds the state the policy actually reads by filtering
    ``state.nodes`` through ``node_counts_toward_card_budget``, so THAT is the set whose children
    exist.  Whenever the two answers differ, the failed parent is simultaneously a leaf the policy
    keeps proposing ``debug`` on and a non-leaf whose Card replay folds to
    ``action_receipt_incomplete`` — and the lane authors a fresh permanently unselectable Card every
    loop turn until the runaway guard ends the run.  Measured offline on a 12-node budget, tombstoned
    and constraint-gated shapes alike: 7 nodes frozen, 89 ``card_added`` of which 84 dead ``debug``
    Cards on ONE parent, ending on ``stuck: 1 action(s) planned for 84 consecutive loop turns without
    creating a node``; the same prefix folds to a normal 12-of-12 finish with this map correct.
    (The escalation to a dead run needs the speculative staging lane — with ``speculation_depth=0``
    the same proposal falls through to a serial build.  The DISAGREEMENT does not: it needs only
    ``card_driven_selection``, and at depth 0 it costs the lane its own staged Card plus a duplicate
    node for the same work.)

    Calling the predicate itself is what makes the two views agree BY CONSTRUCTION.  The 2026-08-05
    first cut of this fix inlined one of its four clauses instead
    (``is_unevaluated_speculative_discard``) and the identical runaway reopened the same day on the
    three it left out — a tombstoned child, a constraint-gated (``feasible=False``) child, and a
    trust-gated (``breed_excluded``) child.  ``events`` may not import ``search``, which is exactly
    why the predicate lives in ``core/models.py``: a fourth spelling of "which children count" is the
    failure this keeps producing, not a layering inconvenience to work around.

    The parent side is deliberately NOT filtered here (see ``_card_debuggable_leaf_candidate_ids``):
    a failed node the policy cannot see stays a candidate, so replay is at worst MORE permissive than
    the policy about the anchor itself.  That direction cannot run away — the lane never proposes
    such a parent, and ``card_selection.eligible_cards`` rechecks every ready debug Card against the
    live ``debug_action`` before it can be claimed, so it fails closed at the claim instead.
    """
    children: dict[int, set[int]] = {}
    for node in st.nodes.values():
        if not node_counts_toward_card_budget(st, node):
            continue
        for parent_id in node.parent_ids:
            children.setdefault(parent_id, set()).add(node.id)
    return {parent_id: frozenset(ids) for parent_id, ids in children.items()}


def _card_debuggable_leaf_candidate_ids(st: RunState) -> set[int]:
    """Failed nodes a debug action may anchor on, BEFORE the has-a-child leaf test.

    Split out of ``_card_debuggable_leaf_ids`` so the one narrow own-work-item exemption in
    ``_card_action_has_live_anchors`` cannot accidentally revive a node that failed a DIFFERENT
    gate (reset, aborted, tombstoned, triage-rejected, or never failed at all).
    """
    return {
        node.id
        for node in st.nodes.values()
        if (
            node.status is NodeStatus.failed
            and not node.tombstoned
            and node.id not in st.aborted_nodes
            and node.error_reason not in {"idea_rejected", "card_dropped"}
        )
    }


def _card_debuggable_leaf_ids(
    st: RunState,
    *,
    candidate_ids: Collection[int] | None = None,
    leaf_children: Mapping[int, Collection[int]] | None = None,
) -> set[int]:
    """Failed leaves that may still anchor one inter-node debug action.

    Failed nodes are deliberately absent from ``RunState.breedable_nodes()``.  Treating ``debug``
    like ``improve`` therefore made every receipt-bound debug Card permanently incomplete.  Keep
    this replay-side shape gate aligned with the policy's non-depth-specific eligibility rules; the
    policy-specific depth bound and deterministic first-leaf choice are rechecked by the Layer-3
    selector before an existing Card is claimed.

    Both halves are accepted precomputed so ``_derive_cards`` — which needs them separately for the
    own-work-item exemption — has exactly ONE spelling of this set rather than an inlined copy.
    """
    if candidate_ids is None:
        candidate_ids = _card_debuggable_leaf_candidate_ids(st)
    if leaf_children is None:
        leaf_children = _card_debug_leaf_children(st)
    return {node_id for node_id in candidate_ids if not leaf_children.get(node_id)}


def _card_action_has_live_anchors(
    card: Card,
    breedable_node_ids: set[int],
    debuggable_leaf_ids: set[int],
    *,
    debuggable_leaf_candidate_ids: Collection[int] = (),
    debuggable_leaf_children: Mapping[int, Collection[int]] | None = None,
    own_work_item_ids: Collection[int] = (),
) -> bool:
    """Whether the bounded action has one executable operator/parent shape right now.

    The three keyword arguments carry the ONE exemption a debug Card is owed against its own work
    item (see the ``debug`` branch).  They default to empty, so a caller that supplies only the
    three positional sets gets exactly the historical answer.
    """
    operator = card.operator
    parent_ids = list(card.parent_ids or [])
    if card.parent_id is not None:
        if parent_ids and parent_ids[0] != card.parent_id:
            return False
        if not parent_ids:
            parent_ids = [card.parent_id]
    if len(parent_ids) != len(set(parent_ids)):
        return False
    if operator == "draft":
        return not parent_ids
    if operator in {"improve", "expand"}:
        return len(parent_ids) == 1 and parent_ids[0] in breedable_node_ids
    if operator == "debug":
        # A failed node can never be breedable.  It is executable only while it is still a current
        # failed leaf; once reset, aborted, rejected by triage, tombstoned, or given a child, replay
        # closes this Card again.  Layer 3 further narrows this set to the policy's first eligible
        # failed leaf under its configured debug-depth bound.
        if len(parent_ids) != 1:
            return False
        parent_id = parent_ids[0]
        if parent_id in debuggable_leaf_ids:
            return True
        # …with ONE exemption, and it is the whole reason speculation could not be turned on: a
        # receipt-bound debug Card's OWN work item is a child of the node it debugs.  So the instant
        # that node existed, this Card's own anchor died and it folded to
        # `action_receipt_incomplete`.  Nothing noticed while speculation was off, because the
        # ordinary lane never re-checks a Card after its node exists; the L5 freshness gate does —
        # that is its whole job — so every speculative debug prefetch was superseded on sight.  A
        # Card's own work item must not disqualify its own parent.  EVERY OTHER child still does,
        # and the parent must still be a live failed node in its own right, so this opens no hole:
        # a debug Card whose parent has a real sibling child stays closed exactly as before.
        if debuggable_leaf_children is None or parent_id not in debuggable_leaf_candidate_ids:
            return False
        blocking = set(debuggable_leaf_children.get(parent_id, ())) - set(own_work_item_ids)
        return not blocking
    if operator == "merge":
        return len(parent_ids) == 2 and all(
            parent_id in breedable_node_ids for parent_id in parent_ids
        )
    return False


def _card_action_freshness(st: RunState, card: Card) -> str:
    """Compare one action's exact lifecycle fences with the current replay state.

    Missing legacy fences are ``unknown`` rather than silently rebound to the latest node attempt.
    A known-dead anchor or a changed best/attempt is ``stale`` even when another legacy fence is
    missing, keeping the future queue fail closed while old card shadows remain readable.
    """
    parent_ids = list(card.parent_ids or [])
    if card.parent_id is not None:
        if parent_ids and parent_ids[0] != card.parent_id:
            parent_state = "stale"
        else:
            if not parent_ids:
                parent_ids = [card.parent_id]
            parent_state = "current"
    else:
        parent_state = "current"
    if len(parent_ids) != len(set(parent_ids)):
        parent_state = "stale"
    expected_parent_keys = {str(parent_id) for parent_id in parent_ids}
    parent_nodes = {parent_id: st.nodes.get(parent_id) for parent_id in parent_ids}
    if any(node is None or node.tombstoned or parent_id in st.aborted_nodes
           for parent_id, node in parent_nodes.items()):
        parent_state = "stale"
    elif card.parent_generations is None:
        if parent_state != "stale":
            parent_state = "unknown"
    elif set(card.parent_generations) != expected_parent_keys:
        parent_state = "stale"
    elif any(
        type(card.parent_generations.get(str(parent_id))) is not int
        or card.parent_generations[str(parent_id)] != node.attempt
        for parent_id, node in parent_nodes.items()
    ):
        parent_state = "stale"

    if card.scored_against is None:
        if card.scored_against_empty:
            score_state = (
                "current"
                if card.scored_against_generation is None and st.best_node_id is None
                else "stale"
            )
        else:
            score_state = "unknown"
    else:
        scored_node = st.nodes.get(card.scored_against)
        if (scored_node is None or scored_node.tombstoned
                or card.scored_against in st.aborted_nodes
                or st.best_node_id != card.scored_against):
            score_state = "stale"
        elif card.scored_against_generation is None:
            score_state = "unknown"
        elif card.scored_against_generation != scored_node.attempt:
            score_state = "stale"
        else:
            score_state = "current"

    states = {parent_state, score_state}
    return "stale" if "stale" in states else "unknown" if "unknown" in states else "current"


def _card_sidecar_subject(st: RunState, d: dict, node_to_card: dict[int, str], *,
                          legacy_reproposed_nodes: set[int] | None = None,
                          cross_run: bool = False) -> str | None:
    """Resolve one sidecar only when its exact proposal/lifecycle subject still owns the node."""
    raw_node_id = d.get("node_id")
    if type(raw_node_id) is not int or raw_node_id < 0:
        return None
    node = st.nodes.get(raw_node_id)
    card_id = node_to_card.get(raw_node_id)
    if node is None or card_id is None or node.idea is None:
        return None
    if node.tombstoned or node.id in st.aborted_nodes:
        return None

    has_ref = "proposal_ref" in d
    has_generation = "generation" in d
    if not has_ref and not has_generation:
        # Historical rows predate exact bindings. Preserve only their original generation-0 behavior;
        # a reproposed rejection explicitly refers to the discarded proposal, and its sibling legacy
        # cross-run row is equally ambiguous for the replacement occupying that slot.
        if node.attempt != 0 or d.get("action") == "reproposed":
            return None
        if cross_run and legacy_reproposed_nodes and raw_node_id in legacy_reproposed_nodes:
            return None
        return card_id

    ref = d.get("proposal_ref")
    generation = d.get("generation")
    if (type(generation) is not int or generation < 0 or generation != node.attempt
            or not isinstance(ref, dict) or set(ref) != {"v", "digest"}
            or ref.get("v") != 1 or not isinstance(ref.get("digest"), str)):
        return None
    expected = idea_proposal_digest(node.idea)
    if expected is None or ref["digest"] != expected:
        return None
    # A modern `reproposed` rejection is deliberately bound to the discarded original. Even if a buggy
    # writer duplicated the digest, never annotate the replacement card with that negative verdict.
    if d.get("action") == "reproposed":
        return None
    return card_id


def _card_novelty_projection(st: RunState, d: dict) -> dict:
    def _text(key: str) -> str | None:
        value = d.get(key)
        return value[:200] if isinstance(value, str) else None

    near_node = d.get("near_node")
    if type(near_node) is not int or near_node < 0:
        near_node = None
    projection = {
        "grade": _text("grade"),
        "level": d.get("level") if type(d.get("level")) is int and 0 <= d["level"] <= 16 else None,
        "near_node": near_node,
        "recommendation": _text("recommendation"),
    }
    near_generation = d.get("near_generation")
    if type(near_generation) is int and near_generation >= 0:
        projection["near_generation"] = near_generation
    if "proposal_ref" in d or "generation" in d:
        # a modern near-node reference names a lifecycle, not a reusable numeric slot.
        # Never let a reset, tombstone, abort, absent row, or malformed generation re-home the verdict
        # onto whichever proposal happens to occupy that id at the end of replay.
        near = st.nodes.get(near_node) if near_node is not None else None
        if (near is None or type(near_generation) is not int or near_generation < 0
                or near.attempt != near_generation or near.tombstoned
                or near.id in st.aborted_nodes):
            projection["near_node"] = None
    return projection


def _card_cross_run_projection(d: dict) -> dict:
    matched = []
    for item in (d.get("matched_concepts") if isinstance(d.get("matched_concepts"), list) else [])[:64]:
        if isinstance(item, str) and item and len(item) <= 256 and item not in matched:
            matched.append(item)
    raw_runs = d.get("prior_runs") if isinstance(d.get("prior_runs"), list) else []
    prior_runs = []
    runs_lossy = len(raw_runs) > 64
    for item in raw_runs[:64]:
        if not isinstance(item, dict):
            runs_lossy = True
            continue
        valid, bounded = _bounded_card_enrichment(item)
        if valid and isinstance(bounded, dict):
            prior_runs.append(bounded)
            runs_lossy = runs_lossy or bounded != item
        else:
            runs_lossy = True

    def _count(key):
        value = d.get(key)
        return value if type(value) is int and 0 <= value <= (1 << 53) - 1 else None

    total = _count("prior_runs_total")
    omitted = _count("prior_runs_omitted")
    projection_drops = max(0, len(raw_runs) - len(prior_runs))
    if total is not None:
        # A bounded card can retain fewer rows than the durable receipt. Never project the producer's
        # pre-projection zero as if this now-truncated view were exact.
        omitted = max(omitted or 0, max(0, total - len(prior_runs)))
    elif omitted is not None and projection_drops:
        omitted = min((1 << 53) - 1, omitted + projection_drops)
    declared_complete = d.get("prior_runs_complete") is True
    complete = bool(
        declared_complete and not runs_lossy and len(prior_runs) == len(raw_runs)
        and total == len(prior_runs) and omitted == 0
    )
    raw_source = d.get("concept_source")
    valid, concept_source = _bounded_card_enrichment(raw_source) if isinstance(raw_source, dict) else (False, {})
    if not valid or not isinstance(concept_source, dict):
        concept_source = {}
    # Completeness is affirmative evidence. Any malformed/truncated source receipt becomes explicitly
    # partial rather than looking exact merely because the retained runs are well formed.
    if (not isinstance(raw_source, dict) or len(raw_source) > 64 or concept_source != raw_source
            or raw_source.get("source_complete") is not True):
        concept_source["source_complete"] = False
    else:
        concept_source["source_complete"] = True
    return {
        "v": d.get("v") if type(d.get("v")) is int else None,
        "matched_concepts": matched,
        "prior_runs": prior_runs,
        "prior_runs_total": total,
        "prior_runs_omitted": omitted,
        "prior_runs_complete": complete,
        "concept_source": concept_source,
    }


_CARD_NODE_CONCEPT_PROVENANCE = _INHERITABLE_CONCEPT_PROVENANCE | {
    # display-only: the card projection shows a low-trust taxonomy, it does not INHERIT through it.
    NODE_CONCEPT_PROVENANCE_UNTRUSTED,
}


def _card_node_concept_projection(st: RunState, node: Node) -> tuple[list[str], CardConceptSource]:
    """Project one exact node owner from the already-finalized concept read model."""
    memberships = getattr(st, "node_concepts", None)
    membership_map_valid = isinstance(memberships, dict)
    membership_present = membership_map_valid and node.id in memberships
    raw_membership = memberships.get(node.id) if membership_present else []
    tags, overflow, invalid = bounded_raw_concept_values(raw_membership)

    receipts = getattr(st, "node_concept_materialization_receipts", None)
    receipt_map_valid = isinstance(receipts, dict)
    raw_receipt = receipts.get(node.id, _MISSING) if receipt_map_valid else None
    receipt = (
        normalized_concept_materialization_receipt(raw_receipt)
        if raw_receipt is not _MISSING else None
    )
    receipt_valid = receipt_map_valid and (
        raw_receipt is _MISSING or receipt is not None)
    reasons: set[ConceptMaterializationReason] = set(
        receipt["reasons"] if receipt is not None else ())
    if overflow:
        reasons.add(CONCEPTS_PER_NODE_CAP_REASON)
    if invalid or (membership_present and not isinstance(raw_membership, list)):
        reasons.add(CONCEPT_INVALID_ID_REASON)
    materialization_receipt = concept_materialization_receipt(reasons)

    provenance_map = getattr(st, "node_concept_provenance", None)
    raw_provenance = provenance_map.get(node.id) if isinstance(provenance_map, dict) else None
    provenance_known = raw_provenance in _CARD_NODE_CONCEPT_PROVENANCE
    provenance = (
        raw_provenance if provenance_known else
        NODE_CONCEPT_PROVENANCE_UNTRUSTED if raw_provenance is not None else None
    )
    # `[]` with membership_present=True and no receipt is an exact empty set.  The same
    # value with an absent key, a corrupt receipt, or an unavailable delta is explicitly incomplete.
    source = CardConceptSource(
        kind="node",
        node_id=node.id,
        node_generation=node.attempt,
        provenance=provenance,
        membership_present=membership_present,
        complete=(membership_present and membership_map_valid and provenance_known
                  and receipt_valid and materialization_receipt is None),
        receipt_valid=receipt_valid,
        materialization_receipt=materialization_receipt,
    )
    return tags, source


def _native_first(native: list, shadow: list) -> list[tuple[bool, dict]]:
    """Pair every Card-family row with whether it is NATIVE, native rows FIRST (doc 25 EV-13).

    `st.cards` subsumes the removed hypothesis board, so each card event has a frozen hypothesis twin
    that old logs still replay. Two phases of the derivation therefore walk both families, and both
    depend on the SAME ordering rule: dedup is first-wins, so a real `card_added` must precede its
    hypothesis twin or the twin claims the id and the native receipt is lost. That rule was spelled
    out as a literal concatenation at each site, which is two places for one invariant to drift.

    The `native` FLAG is deliberately NOT normalized away, and this is where EV-13's framing —
    "normalize hypotheses into synthetic card-shaped rows once, so the derivation reasons over one
    input family" — does not survive contact with the code. The two families are not one family in
    different clothes: only a native row can carry an ownership receipt (`_card_added_snapshot` /
    `_card_added_ownership` are meaningless on a hypothesis row and a shadow row must resolve to
    `receipt_valid=False`), only a shadow row is excluded by `ambiguous_seeds`, and the two produce
    different `card_origins` provenance. A synthetic row that made a hypothesis LOOK native would
    have to carry a "not really native" bit anyway — the same flag, one layer further from the branch
    that reads it. So what is shared is the ORDERING, and that is what this owns.
    """
    return [(True, row) for row in native] + [(False, row) for row in shadow]


@dataclass(frozen=True)
class _CardIdentity:
    """Which spellings may name a Card, decided over the whole log BEFORE any Card exists.

    Every later phase consults this and none of them may extend it: a decision about identity that
    depended on how far the derivation had got would depend on event order through the back door.
    """
    conflicted_native_ids: set[str]
    ambiguous_seeds: set[str]
    owner_by_statement: dict[str, str]
    seed_owner: dict[str, str]


@dataclass
class _CardLedger:
    """The Cards under construction plus the provenance tables that travel with them.

    ``cards`` is keyed by canonical id; ``card_origins`` records which seed produced each row;
    ``action_owned_cards`` is the set whose action block came from a concrete proposal; and the two
    counter tables are what step 9 turns into `identity` / `selection_provenance`. They are one
    object because the merge fold has to rewrite all four consistently — carrying them as four
    parameters is how one of them gets left behind.
    """
    cards: dict[str, Card] = dataclasses.field(default_factory=dict)
    card_origins: dict[str, str] = dataclasses.field(default_factory=dict)
    action_owned_cards: set[str] = dataclasses.field(default_factory=set)
    card_registrations: dict[str, dict] = dataclasses.field(default_factory=dict)
    action_owners: dict[str, dict] = dataclasses.field(default_factory=dict)

    def record_registration(self, card_id: str, *, valid: bool, digest: str | None) -> None:
        row = self.card_registrations.setdefault(
            card_id, {"count": 0, "valid_count": 0, "digest": None})
        row["count"] = min(257, row["count"] + 1)
        if valid:
            row["valid_count"] = min(257, row["valid_count"] + 1)
            row["digest"] = digest if row["valid_count"] == 1 else None

    def record_action_owner(self, card_id: str, source: str, *, complete: bool) -> None:
        row = self.action_owners.setdefault(
            card_id, {"count": 0, "sources": set(), "all_complete": True})
        row["count"] = min(257, row["count"] + 1)
        row["sources"].add(source)
        row["all_complete"] = row["all_complete"] and complete


@dataclass(frozen=True)
class _CardAliases:
    """The resolved alias graph: hash -> native identity bridges plus `card_merged` edges."""
    alias: dict[str, str]
    identity_bridge_ids: frozenset[str]
    merged_stmt: dict[str, str]

    def canon(self, x: str) -> str:                 # resolve alias chains a->b->c, cycle-safe
        seen: set[str] = set()
        while x in self.alias and x not in seen:
            seen.add(x)
            x = self.alias[x]
        return x


def _card_id(value) -> str | None:
    # DELIBERATELY not `_card_replay_id`, and the difference is WHERE the length bound lands: this
    # bounds the STRIPPED id at 256, admission rejects a raw string longer than 256 before stripping.
    # So a padded 300-character spelling with a 250-character core is a usable control id here and
    # not admissible as a `card_added` id. The derive side reads ids from places admission never
    # bounded (`Idea.card_id`, operator pin/edit keys, a `card_ranked` order entry), so unifying the
    # two would silently retire controls on historical logs. Two names, one difference, both stated.
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    return bounded if bounded and len(bounded) <= 256 and bounded.isprintable() else None


def _node_parent_generations(st: RunState, node: Node) -> dict[str, int] | None:
    parents = list(node.parent_ids or [])
    if any(parent_id not in st.nodes for parent_id in parents):
        return None
    return {str(parent_id): st.nodes[parent_id].attempt for parent_id in parents}


def _card_identity_map(st: RunState) -> _CardIdentity:
    """Phase 0: decide which ids are usable identities/controls, over the whole durable log."""
    # A legacy hypothesis shadow uses hypothesis_id(statement), while a staged card has an independent
    # stable id. Bridge the hash only when there is exactly one native id for that seed. Two different
    # native ids may carry different action blocks; guessing an owner would silently lose work, so the
    # ambiguous hash row stays audit-only and neither native card inherits hash-addressed controls.
    native_ids_by_statement: dict[str, list[str]] = {}
    statements_by_native_id: dict[str, list[str]] = {}
    statements_by_seed_hash: dict[str, list[str]] = {}
    seed_hash_by_statement: dict[str, str] = {}

    def _register_card_identity(statement: str, raw_native_id=None) -> None:
        if not statement:
            return
        seed_id = hypothesis_id(statement)
        statement_id = hypothesis_statement_digest(statement)
        seed_hash_by_statement[statement_id] = seed_id
        seed_statements = statements_by_seed_hash.setdefault(seed_id, [])
        if statement_id not in seed_statements:
            seed_statements.append(statement_id)
        cid = _card_id(raw_native_id)
        if cid is None or cid == seed_id:
            return
        native_ids = native_ids_by_statement.setdefault(statement_id, [])
        if cid not in native_ids:
            native_ids.append(cid)
        native_statements = statements_by_native_id.setdefault(cid, [])
        if statement_id not in native_statements:
            native_statements.append(statement_id)

    # identity conflicts can enter through a staged card, a legacy hypothesis, or a node
    # whose Idea already carries card_id. Scan all three durable sources before creating any card; only
    # scanning card_added would let node-only logs reuse one stable id and silently conflate evidence.
    for d in st.cards_added:
        try:
            statement = str(d.get("statement") or "").strip()
            _register_card_identity(statement, d.get("id"))
        except Exception:  # noqa: BLE001 - malformed staging rows remain audit-only
            continue
    for d in st.hypotheses_added:
        try:
            _register_card_identity(str(d.get("statement") or "").strip())
        except Exception:  # noqa: BLE001 - malformed legacy rows remain audit-only
            continue
    for node in st.nodes.values():
        try:
            statement = str(node.idea.hypothesis or "").strip() if node.idea is not None else ""
            _register_card_identity(
                statement, node.idea.card_id if node.idea is not None else None)
        except Exception:  # noqa: BLE001 - malformed historical nodes remain independently visible
            continue
    namespace_conflicts = {
        cid for cid, statement_ids in statements_by_native_id.items()
        if any(target not in statement_ids for target in statements_by_seed_hash.get(cid, ()))
    }
    # Reusing one explicit id for two full statements is unrepresentable and must be suppressed. A
    # different case is an explicit id that merely happens to equal another statement's legacy short
    # hash: both explicit cards can still be preserved, but that shared spelling is unsafe for controls.
    conflicted_native_ids = {
        cid for cid, statement_ids in statements_by_native_id.items() if len(statement_ids) > 1
    }
    ambiguous_statement_ids = {
        statement_id for statement_id, native_ids in native_ids_by_statement.items()
        if (len(native_ids) != 1 or native_ids[0] in conflicted_native_ids
            or seed_hash_by_statement[statement_id] in namespace_conflicts
            or len(statements_by_seed_hash.get(seed_hash_by_statement[statement_id], ())) != 1)
    }
    owner_by_statement = {
        statement_id: native_ids[0]
        for statement_id, native_ids in native_ids_by_statement.items()
        if statement_id not in ambiguous_statement_ids
    }
    seed_owner = {
        seed_hash_by_statement[statement_id]: owner
        for statement_id, owner in owner_by_statement.items()
    }
    ambiguous_seeds = {
        seed_hash_by_statement[statement_id] for statement_id in ambiguous_statement_ids
    } | {
        seed_id for seed_id, statement_ids in statements_by_seed_hash.items() if len(statement_ids) > 1
    } | namespace_conflicts
    return _CardIdentity(
        conflicted_native_ids=conflicted_native_ids, ambiguous_seeds=ambiguous_seeds,
        owner_by_statement=owner_by_statement, seed_owner=seed_owner,
    )


def _seed_cards_from_receipts(
        st: RunState, identity: _CardIdentity, ledger: _CardLedger) -> None:
    cards = ledger.cards
    card_origins = ledger.card_origins
    action_owned_cards = ledger.action_owned_cards
    conflicted_native_ids = identity.conflicted_native_ids
    ambiguous_seeds = identity.ambiguous_seeds
    owner_by_statement = identity.owner_by_statement
    _record_registration = ledger.record_registration
    _record_action_owner = ledger.record_action_owner

    # 1) explicitly-added cards — may start with no evidence. Coerce defensively (engine/control events
    #    arrive verbatim; one malformed entry must not brick every fold). The compatibility projection
    #    also MIRRORS the removed `_derive_hypotheses` by seeding from the
    #    engine-populated `hypotheses_added` (deep-research/human directions), so a node-less hypothesis
    #    still becomes a card — `st.cards` now SUBSUMES the removed `st.hypotheses` board (its cards-only
    #    replacement). `card_*` first so a real card_added (explicit id/source) wins the id over its
    #    hypothesis twin (dedup = first wins).
    for native_row, d in _native_first(st.cards_added, st.hypotheses_added):
        try:
            stmt = str(d.get("statement", "")).strip()
            seed_id = hypothesis_id(stmt) if stmt else ""
            statement_id = hypothesis_statement_digest(stmt) if stmt else ""
            raw_id = d.get("id")
            raw_cid = _card_id(raw_id) or seed_id
            if raw_cid in conflicted_native_ids:
                continue
            # never materialize a third, hash-addressed queue item beside ambiguous native
            # cards. The raw hypothesis/card event remains the durable audit receipt.
            if seed_id in ambiguous_seeds and (not native_row or raw_cid == seed_id):
                continue
            cid = owner_by_statement.get(statement_id, raw_cid) if raw_cid == seed_id else raw_cid
            if not cid or len(cid) > 256:
                continue
            try:
                at_node = int(d.get("at_node", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                at_node = 0
            if not 0 <= at_node <= (1 << 31) - 1:
                at_node = 0
            snapshot, owns_action = _card_added_snapshot(d) if native_row else ({}, False)
            receipt_valid, action_complete, action_digest = (
                _card_added_ownership(d, cid, stmt, snapshot, owns_action=owns_action)
                if native_row else (False, False, None)
            )
            if native_row and receipt_valid and snapshot.get("footprint") is not None:
                # Authority is derived only after the immutable native receipt validates. Event data
                # can declare quantities but cannot self-assign pinned/finalized/proposed provenance.
                snapshot["footprint"] = {
                    **snapshot["footprint"], "proposed_by": "researcher",
                }
            if native_row:
                _record_registration(cid, valid=receipt_valid, digest=action_digest)
                if owns_action:
                    _record_action_owner(cid, "card_added", complete=action_complete)
            if cid in cards:
                continue
            cards[cid] = Card(
                id=cid, statement=stmt, seed_statement=stmt,
                source=str(d.get("source") or "human"),   # mirror _derive_hypotheses' default
                rationale=str(d.get("rationale", ""))[:400], created_at_node=at_node,
                **snapshot,
            )
            card_origins[cid] = "card_added_unbound" if native_row else "hypothesis_shadow"
            if owns_action:
                action_owned_cards.add(cid)
        except Exception:  # noqa: BLE001 — one bad record must not brick the fold
            continue


def _link_cards_to_nodes(
        st: RunState, identity: _CardIdentity, ledger: _CardLedger) -> None:
    cards = ledger.cards
    card_origins = ledger.card_origins
    action_owned_cards = ledger.action_owned_cards
    action_owners = ledger.action_owners
    conflicted_native_ids = identity.conflicted_native_ids
    ambiguous_seeds = identity.ambiguous_seeds
    owner_by_statement = identity.owner_by_statement
    _record_action_owner = ledger.record_action_owner

    # 2) derive/link from nodes that state a hypothesis (evidence = the node). Link by `idea.card_id`
    #    (Layer-1a stable id) when present, else the statement hash (legacy/derived fallback), mirroring
    #    `_derive_hypotheses`. `sorted(st.nodes)` keeps evidence order == the hypothesis shadow's.
    for nid in sorted(st.nodes):
        n = st.nodes[nid]
        if n.idea is None:
            continue
        stmt = (n.idea.hypothesis or "").strip()
        seed_id = hypothesis_id(stmt) if stmt else ""
        statement_id = hypothesis_statement_digest(stmt) if stmt else ""
        explicit_card_id = (n.idea.card_id or "").strip()
        # `card_id` is work-item identity, not belief identity. Two native actions that
        # reuse the exact hypothesis wording now land in separate Cards, whereas the removed
        # statement-keyed ledger accumulated both evidence rows. That fragments verdicts/lessons and
        # contradicts the Researcher instruction to reuse wording. Preserve a grouped belief projection
        # keyed by the seed hash while keeping the native action identities distinct.
        raw_cid = explicit_card_id or seed_id
        if explicit_card_id in conflicted_native_ids:
            continue
        if not explicit_card_id and seed_id in ambiguous_seeds:
            continue  # no exact native identity: attaching legacy evidence would be an arbitrary guess
        cid = owner_by_statement.get(statement_id, raw_cid) if raw_cid == seed_id else raw_cid
        if not cid:
            continue
        existing_action = action_owners.get(cid)
        if existing_action is None or "card_added" not in existing_action["sources"]:
            _record_action_owner(cid, "node", complete=False)
        node_concept_tags, node_concept_source = _card_node_concept_projection(st, n)
        c = cards.get(cid)
        if c is None:
            c = Card(id=cid, statement=stmt, seed_statement=stmt, source="researcher",
                     rationale=(n.idea.rationale or "")[:400], created_at_node=n.id,
                     operator=n.idea.operator, params=dict(n.idea.params or {}),
                     space={k: list(v) for k, v in (n.idea.space or {}).items()},
                     eval_profile=n.idea.eval_profile, eval_timeout=n.idea.eval_timeout,
                     concept_tags=node_concept_tags,
                     concept_source=node_concept_source,
                     provenance_tier=node_concept_source.provenance,
                     parent_id=(n.parent_ids[0] if n.parent_ids else None),
                     parent_ids=list(n.parent_ids or []),
                     parent_generations=_node_parent_generations(st, n))
            cards[cid] = c
            card_origins[cid] = "node_card_id" if explicit_card_id else "node_statement_hash"
            action_owned_cards.add(cid)
        elif not c.evidence and cid not in action_owned_cards:
            # card_added is intentionally thin. Backfill its missing action block from the
            # earliest linked node; otherwise the normal card_added -> node_created staging path leaves a
            # permanently substance-free card. Copy the whole block atomically (including legitimate
            # empties) so later evidence cannot synthesize a chimera from several proposals.
            c.operator = n.idea.operator
            c.params = dict(n.idea.params or {})
            c.space = {k: list(v) for k, v in (n.idea.space or {}).items()}
            c.eval_profile = n.idea.eval_profile
            c.eval_timeout = n.idea.eval_timeout
            c.parent_id = n.parent_ids[0] if n.parent_ids else None
            c.parent_ids = list(n.parent_ids or [])
            c.parent_generations = _node_parent_generations(st, n)
            action_owned_cards.add(cid)
        if c.concept_source is None or c.concept_source.kind != "node":
            # the first linked node is the exact action/evidence owner.  Later evidence may
            # have classifier/operator tags of its own, but folding those into one card would create a
            # provenance lie.  Node ids are visited in sorted order, so ownership is replay-order stable.
            c.concept_tags = node_concept_tags
            c.concept_source = node_concept_source
            c.provenance_tier = node_concept_source.provenance
        if n.id not in c.evidence:
            c.evidence.append(n.id)


def _card_merge_aliases(st: RunState, identity: _CardIdentity) -> _CardAliases:
    ambiguous_seeds = identity.ambiguous_seeds
    seed_owner = identity.seed_owner

    # 2b) apply `card_merged` events (fold each ALIAS card's evidence into its CANONICAL) — fully
    #     DETERMINISTIC (no LLM; the decision was recorded by the engine), order-tolerant, cycle-safe.
    #     Mirrors `_derive_hypotheses` 2b exactly, reusing the same `_canon` alias-chain resolution.
    alias: dict[str, str] = {}
    for seed_id, owner in seed_owner.items():
        if seed_id != owner:
            alias[seed_id] = owner
    identity_bridge_ids = frozenset(alias)
    # Edges written by the merge loop below, kept apart from the hash->native bridge seeding above so
    # a conflict can be resolved WITHOUT changing what a bridge means. See the `min()` note at the
    # write site: last-write-wins there made `fold(perm(events))` differ, breaking invariant 5.
    merge_alias: dict[str, str] = {}
    merged_stmt: dict[str, str] = {}
    for native_merge, d in _native_first(st.cards_merged, st.hypotheses_merged):
        try:
            raw_canonical = d.get("canonical")
            raw_aliases = d.get("aliases")
            if not isinstance(raw_aliases, list):
                continue
            raw_canon = _card_id(raw_canonical)
            if raw_canon is None or (not native_merge and raw_canon in ambiguous_seeds):
                continue
            canon = seed_owner.get(raw_canon, raw_canon)
            s = str(d.get("statement", "")).strip()
            if s:
                merged_stmt[canon] = s
            seen_aliases: set[str] = set()
            for raw_alias in raw_aliases[:256]:
                a = _card_id(raw_alias)
                if a is None or (not native_merge and a in ambiguous_seeds):
                    continue
                resolved_alias = seed_owner.get(a, a)
                if resolved_alias != canon and resolved_alias not in seen_aliases:
                    seen_aliases.add(resolved_alias)
                    # Two receipts naming the same alias with DIFFERENT canonicals (X->A and X->B)
                    # used to resolve by last-event-wins, so `fold(perm(events))` sent X to A or to B
                    # depending on byte order — reproduced, and a direct violation of invariant 5 that
                    # the section header three comments up already claims to hold. Pick a COMMUTATIVE
                    # winner instead: the lexicographically smallest canonical, exactly the hardening
                    # `_on_card_concept_consolidation` applies to its own rename map and for the same
                    # stated reason. The engine records each merge once, so a conflict only arises in
                    # an adversarial or spliced log; this just makes the fold total on one. Resolving
                    # inside `merge_alias` keeps hash -> native ownership intact: a bridge edge is
                    # still retargeted by a merge exactly as before, but the merge decision itself no
                    # longer depends on order.
                    prior = merge_alias.get(resolved_alias)
                    merge_alias[resolved_alias] = canon if prior is None else min(prior, canon)
                    alias[resolved_alias] = merge_alias[resolved_alias]
        except Exception:  # noqa: BLE001 — one bad merge record must not brick the fold
            continue
    return _CardAliases(
        alias=alias, identity_bridge_ids=identity_bridge_ids, merged_stmt=merged_stmt)


def _card_control_ids(identity: _CardIdentity, ledger: _CardLedger) -> dict[str, set[str]]:
    cards = ledger.cards
    ambiguous_seeds = identity.ambiguous_seeds

    # Legacy hypothesis controls name a statement hash while a modern card may have a stable independent
    # id. Carry every pre-merge id and seed hash forward to the final canonical card.
    control_ids: dict[str, set[str]] = {}
    for cid, c in cards.items():
        ids = {cid}
        if c.seed_statement:
            ids.add(hypothesis_id(c.seed_statement))
        # A spelling shared by a native id and another statement's legacy hash cannot identify which
        # card an old control intended. Preserve both cards, but apply no ambiguous control by guessing.
        control_ids[cid] = {control_id for control_id in ids if control_id not in ambiguous_seeds}
    return control_ids


def _fold_merged_cards(identity: _CardIdentity, ledger: _CardLedger, aliases: _CardAliases,
                       control_ids: dict[str, set[str]]) -> dict[str, set[str]]:
    """Union every alias chain onto its canonical Card. Returns the rewritten control-id map."""
    cards = ledger.cards
    card_origins = ledger.card_origins
    action_owned_cards = ledger.action_owned_cards
    action_owners = ledger.action_owners
    ambiguous_seeds = identity.ambiguous_seeds
    alias = aliases.alias
    identity_bridge_ids = aliases.identity_bridge_ids
    merged_stmt = aliases.merged_stmt
    _canon = aliases.canon

    if alias:
        folded: dict[str, Card] = {}
        folded_control_ids: dict[str, set[str]] = {}
        folded_origins: dict[str, str] = {}
        folded_action_owners: dict[str, dict] = {}
        grouped: dict[str, list[str]] = {}
        for cid in sorted(cards):
            grouped.setdefault(_canon(cid), []).append(cid)
        for tid in sorted(grouped):
            members = grouped[tid]
            # if a merge names no materialized canonical row, event insertion order must not
            # choose the surviving action/concept owner. Prefer a canonical action; otherwise choose the
            # lexically first concrete action and copy its WHOLE block plus concept receipt together.
            action_candidates = [cid for cid in members if cid in action_owned_cards]
            action_owner_id = (
                tid if tid in action_owned_cards else
                action_candidates[0] if action_candidates else
                tid if tid in cards else members[0]
            )
            base_id = tid if tid in cards else action_owner_id
            tgt = cards[base_id].model_copy(deep=True)
            if action_owner_id != base_id:
                action_owner = cards[action_owner_id].model_copy(deep=True)
                for field in (
                    "operator", "params", "space", "eval_profile", "eval_timeout",
                    "concept_tags", "concept_source", "provenance_tier",
                    "parent_id", "parent_ids", "parent_generations",
                    "scored_against", "scored_against_generation", "scored_against_empty",
                    "footprint", "steering_context",
                ):
                    setattr(tgt, field, getattr(action_owner, field))
            tgt.id = tid
            if tid in merged_stmt:
                tgt.statement = merged_stmt[tid]    # DISPLAY statement; seed remains the join key
            tgt.evidence = sorted({
                evidence for cid in members for evidence in cards[cid].evidence
            })
            tgt.aliases = sorted({
                alias_id for cid in members
                for alias_id in ([cid] if cid != tid else []) + list(cards[cid].aliases)
                if alias_id != tid
            })
            folded[tid] = tgt
            folded_origins[tid] = card_origins.get(tid, "merge")
            owner_rows = [action_owners[cid] for cid in members if cid in action_owners]
            if owner_rows:
                folded_action_owners[tid] = {
                    "count": min(257, sum(row["count"] for row in owner_rows)),
                    "sources": set().union(*(row["sources"] for row in owner_rows)),
                    "all_complete": all(row["all_complete"] for row in owner_rows),
                }
            target_controls = folded_control_ids.setdefault(
                tid, {tid} if tid not in ambiguous_seeds else set())
            for cid in members:
                target_controls.update(control_ids.get(cid, set()))
        for alias_id in alias:
            target_id = _canon(alias_id)
            target = folded.get(target_id)
            # Hash -> native edges are lookup/control bridges, not work items merged into this Card.
            # Keep them in ``control_ids`` below, but do not leak those automatic spellings through the
            # public ``Card.aliases`` audit field. Explicit/materialized merge members were added above.
            if (target is not None and alias_id not in identity_bridge_ids
                    and alias_id != target_id and alias_id not in target.aliases):
                target.aliases.append(alias_id)
                target.aliases.sort()
                target_controls = folded_control_ids.setdefault(
                    target_id, {target_id} if target_id not in ambiguous_seeds else set(),
                )
                if alias_id not in ambiguous_seeds:
                    target_controls.add(alias_id)
        cards = folded
        control_ids = folded_control_ids
        card_origins = folded_origins
        action_owners = folded_action_owners
        # The merge fold REPLACES all four tables rather than mutating them, so publish them back
        # onto the ledger together — a phase that kept the pre-merge `cards` would silently keep the
        # pre-merge provenance too.
        ledger.cards = cards
        ledger.card_origins = card_origins
        ledger.action_owners = action_owners
    return control_ids


def _apply_card_verdicts(
        st: RunState, ledger: _CardLedger, control_ids: dict[str, set[str]]) -> None:
    cards = ledger.cards

    # 3) record-setters (sticky SOTA advancers) — the SAME pure helper the hypotheses use, so a card's
    #    verdict is byte-identical to its hash-joined hypothesis.
    _record_setters = _record_setter_ids(st.nodes, st.direction)

    # 4) verdict per card via the SHARED helper (open/testing/supported/tested/abandoned). `is_abandoned`
    #    mirrors the hypothesis: a shadow card keyed by the hypothesis id inherits the abandoned override.
    for c in cards.values():
        c.best_delta, c.verdict, _ = _evidence_verdict(
            c.evidence, st.nodes, st.direction, _record_setters,
            any(control_id in st.hypotheses_abandoned
                for control_id in control_ids.get(c.id, {c.id})))


def _apply_card_drops(st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> dict[str, dict]:
    cards = ledger.cards
    _canon = aliases.canon

    # 5) apply `card_auto_dropped` engine effects and `card_dropped` operator overrides. The card STAYS
    #    visible (like an abandoned hypothesis) — lifecycle `status` shows the 'dropped' lane. Historical
    #    engine-authored `card_dropped` rows are already normalized into the same bounded receipt list.
    dropped: dict[str, dict] = {}
    for d in st.cards_dropped:
        raw_id = d.get("id")
        bounded_id = _card_id(raw_id)
        if bounded_id is None:
            continue
        # CODEX AGENT: canonicalizing historical drop receipts through a later merge transfers one
        # member's terminal lifecycle onto the healthy survivor. Bind closure to merge generation/member
        # identity, or merge evidence without rewriting per-Card lifecycle provenance.
        cid = _canon(bounded_id)
        if cid:
            dropped[cid] = d
    for cid, d in dropped.items():
        c = cards.get(cid)
        if c is not None:
            reason = str(d.get("reason", "") or "")[:400]
            c.dropped_reason = reason or None
            c.dropped_by = str(d.get("dropped_by") or d.get("by") or "engine")
    return dropped


def _card_building_ids(st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> set[str]:
    cards = ledger.cards
    _canon = aliases.canon

    # A build reservation is not evidence yet, so do not append its prospective node id to Card.evidence.
    # Its explicit card_id is nevertheless the durable in-flight ownership link. Resolve it through the
    # same merge/statement aliases as every other card control so a reservation made just before a merge
    # follows the surviving work item. Unknown or malformed ids cannot synthesize a substance-free card.
    # `node_building` — NOT `card_build_requested` — is deliberately where ownership is stamped, and a
    # review proposal to also fold outstanding `card_build_requests[card_builds_done:]` in here was
    # tried and REJECTED. It reads plausible (the engine's `_election_excluded_card_ids` does contain
    # request-head card_ids, so the card cannot be RE-elected) but it inverts what `selection_ready`
    # means for the servicer of that very head: `_prepare_existing_card_claim` requires
    # `card.selection_ready`, and BOTH `_producer_card_reservation` and `_commit_card_build` re-fold
    # AFTER the request is durable. Making the request its own blocker means the producer can never
    # claim the card it was asked to build — every speculative build returns "stale", and
    # `tests/test_card_speculation_engine.py` hangs. The exclusion set prevents a SECOND build; the
    # fold's readiness is what lets the FIRST one (including a crash-recovery re-entry) proceed, so the
    # two are not the same question. `tests/test_card_selection_guard.py::
    # test_an_outstanding_build_request_leaves_its_card_claimable_by_the_head_servicer` locks this.
    building_card_ids: set[str] = set()
    for marker in st.buildings.values():
        if not isinstance(marker, dict):
            continue
        marker_card_id = _card_id(marker.get("card_id"))
        if marker_card_id is None:
            continue
        canonical_id = _canon(marker_card_id)
        if canonical_id in cards:
            building_card_ids.add(canonical_id)
    return building_card_ids


def _apply_card_status(st: RunState, ledger: _CardLedger, dropped: dict[str, dict],
                       building_card_ids: set[str]) -> None:
    cards = ledger.cards

    # 6) lifecycle `status` lane (frozen vocab; DISTINCT from the verdict). Dropped/merged-away wins;
    #    else an explicit node_building.card_id reservation -> building; else a pending node -> running;
    #    else evidence all trust-gated/breed-excluded/infeasible -> gated; else terminal evidence ->
    #    evaluated; no evidence -> proposed.
    for cid, c in cards.items():
        if cid in dropped or c.merged_into:
            c.status = "dropped"
            continue
        ev_nodes = [st.nodes[i] for i in c.evidence if i in st.nodes and not st.nodes[i].tombstoned]
        if cid in building_card_ids:
            c.status = "building"
        elif not ev_nodes:
            c.status = "proposed"
        # every pending Node collapses directly to running, so the frozen coded lane advertised by the
        # model/UI is unreachable. A durable evaluation-start boundary now EXISTS —
        # `events/types.py::EV_NODE_EVAL_STARTED`, folded to `Node.eval_started` by
        # `_on_node_eval_started` — but it is stamped only on speculative attempt-zero lifecycles (see
        # `Node.eval_start_boundary`) and this branch reads neither field, so the reason the lane cannot
        # be derived is now THIS projection rather than missing evidence. Split the pending branch on
        # `eval_started` (and widen the boundary past speculative nodes if the lane must be general), or
        # remove the lane until coded-versus-running can be represented truthfully.
        elif any(n.status is NodeStatus.pending for n in ev_nodes):
            c.status = "running"
        elif all((n.id in st.breed_excluded) or (not n.feasible) for n in ev_nodes):
            c.status = "gated"
        else:
            c.status = "evaluated"


def _apply_card_enrichment(st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> None:
    cards = ledger.cards
    _canon = aliases.canon

    # 6b) LAYER-1b ENRICHMENT — re-home the folded "homeless" signals onto the card + apply explicit
    #     card_enriched deltas. Every source is ALREADY folded (the linking node's Idea, the novelty/
    #     cross-run sidecars) or a main-task card event, so this stays pure/deterministic. Operator
    #     overrides (step 7) run AFTER, so an operator pin always wins over an engine enrichment.
    node_to_card: dict[int, str] = {}
    for cid, c in cards.items():
        for nid in c.evidence:
            node_to_card.setdefault(nid, cid)   # first card claiming a node wins (evidence is per-card)

    # Researcher-proposed footprint + research origin ride the linking node's Idea/Node (earliest wins).
    for c in cards.values():
        for nid in c.evidence:
            n = st.nodes.get(nid)
            if n is None or n.idea is None:
                continue
            if c.footprint is None and n.idea.footprint:
                c.footprint = {**n.idea.footprint, "proposed_by": "researcher"}
            if c.research_origin is None and isinstance(n.research_origin, dict):
                # Modern nodes carry the stable content-addressed memo id. Preserve the old node:<N>
                # spelling only for historical events that predate that additive field.
                from looplab.core.advisory_payloads import valid_advisory_ref
                memo_id = n.research_origin.get("memo_id")
                _at = n.research_origin.get("at_node")
                if valid_advisory_ref(memo_id, "memo"):
                    c.research_origin = memo_id
                elif _at is not None and "memo_id" not in n.research_origin:
                    c.research_origin = f"node:{_at}"
            if c.footprint is not None and c.research_origin is not None:
                break

    # Novelty verdict + cross-run prior — the sidecar signals, keyed by the (prospective -> actual) node
    # id they were emitted for. Last write per node wins; ref-shaped (no verbatim capture on the card).
    # `novelty_events` (near-duplicate rejects — no grade) go FIRST so a richer `novelty_grades` entry
    # for the same node wins on collision instead of being clobbered by the sparse reject.
    legacy_reproposed_nodes = {
        d["node_id"] for d in st.novelty_events
        if type(d.get("node_id")) is int and d.get("action") == "reproposed"
        and "proposal_ref" not in d and "generation" not in d
    }
    for d in list(st.novelty_events) + list(st.novelty_grades):
        cid = _card_sidecar_subject(st, d, node_to_card)
        if cid:
            cards[cid].novelty_verdict = _card_novelty_projection(st, d)
    for d in st.cross_run_priors:
        cid = _card_sidecar_subject(
            st, d, node_to_card, legacy_reproposed_nodes=legacy_reproposed_nodes, cross_run=True)
        if cid:
            cards[cid].cross_run_prior = _card_cross_run_projection(d)

    # Explicit card_enriched deltas — last-write-by-seq. An ALLOW-LIST is the ONLY thing that protects the
    # shadow: a field NOT listed here (id/statement/verdict/status/evidence/best_delta/...) is never
    # touched, so a malformed/hostile delta cannot overwrite a shadow-load-bearing field. Each field is
    # type-guarded and the two numeric coercions are guarded INDIVIDUALLY, so a bad numeric field can
    # never drop a valid sibling field that appears after it in the delta (key-order-independent apply).
    _ENRICH_DICT = {"novelty_verdict", "cross_run_prior", "footprint"}
    _ENRICH_REFS = {"lesson_refs", "claim_refs"}
    _ENRICH_STR = {"research_origin"}
    for d in sorted(st.cards_enriched, key=lambda r: (
            r.get("_seq") if type(r.get("_seq")) is int else -1,
            r.get("_event_index") if type(r.get("_event_index")) is int else -1)):
        try:
            raw_id = d.get("id")
            bounded_id = _card_id(raw_id)
            if bounded_id is None:
                continue
            c = cards.get(_canon(bounded_id))
        except Exception:  # noqa: BLE001 — a malformed id must not brick the fold
            c = None
        if c is None:
            continue
        if {"node_id", "generation", "proposal_ref"} <= set(d):
            # Modern engine enrichment belongs to one exact proposal lifecycle. Resolve through the
            # same node-to-Card authority as novelty/cross-run sidecars, then require its declared Card
            # to be that subject after merge canonicalization.
            subject = _card_sidecar_subject(st, d, node_to_card)
            if subject is None or _canon(bounded_id) != subject:
                continue
        for k, v in d.items():
            if k in _ENRICH_DICT and isinstance(v, dict):
                valid, bounded = _bounded_card_enrichment(v)
                if valid:
                    setattr(c, k, bounded)
            elif k == "concept_tags" and isinstance(v, list):
                if c.concept_source is not None and c.concept_source.kind == "node":
                    continue
                refs: list[str] = []
                for item in v[:64]:
                    if isinstance(item, str) and item not in refs:
                        refs.append(item)
                c.concept_tags = refs
                c.concept_source = _proposal_card_concept_source(
                    "card_enriched", present=True,
                    overflow=d.get("_concept_tags_overflow") is True,
                    invalid=d.get("_concept_tags_invalid") is True,
                )
                # enrichment is proposal metadata, never independent classifier/operator
                # evidence.  Keep the legacy scalar synchronized with the exact owner receipt.
                c.provenance_tier = None
            elif k in _ENRICH_REFS and isinstance(v, list):
                refs: list[str] = []
                for item in v[:64]:
                    if not isinstance(item, str):
                        continue
                    ref = item.strip()[:400]
                    if ref and ref not in refs:
                        refs.append(ref)
                setattr(c, k, refs)
            elif k == "steering_context" and isinstance(v, list):
                context: list[dict] = []
                for item in v[:64]:
                    if not isinstance(item, dict):
                        continue
                    valid, bounded = _bounded_card_enrichment(item)
                    if valid and isinstance(bounded, dict):
                        context.append(bounded)
                c.steering_context = context
            elif k in _ENRICH_STR and v is not None:
                setattr(c, k, str(v)[:400])
            elif k == "foresight_rank" and v is not None:
                try:
                    rank = int(v)
                except (TypeError, ValueError):
                    pass
                else:
                    if not isinstance(v, bool) and 0 <= rank < 256:
                        c.foresight_rank = rank
            elif k == "confidence" and v is not None:
                try:
                    confidence = float(v)
                except (TypeError, ValueError, OverflowError):
                    pass
                else:
                    if (not isinstance(v, bool) and math.isfinite(confidence)
                            and 0.0 <= confidence <= 1.0):
                        c.confidence = confidence


def _apply_card_ranking(st: RunState, identity: _CardIdentity, ledger: _CardLedger,
                        aliases: _CardAliases) -> None:
    cards = ledger.cards
    ambiguous_seeds = identity.ambiguous_seeds
    _canon = aliases.canon

    # Board priority — the explicit `card_ranked` order, else the `hypothesis_ranking` shadow (both stamp
    # the OPEN lane's 0-based position, mirroring `_derive_hypotheses`; None once a card resolves).
    native_card_ranking = st.card_ranking is not None
    order = (st.card_ranking or st.hypothesis_ranking or {}).get("order") or []
    if native_card_ranking:
        # A native card_ranked event owns the foresight projection, including clearing a prior explicit
        # enrichment for cards it no longer ranks. Confidence belongs to the same ranking snapshot, so
        # a rerank without confidence must not leave the previous decision's confidence behind.
        for c in cards.values():
            c.foresight_rank = None
            c.confidence = None
    ranking_confidence = (
        st.card_ranking.get("confidence")
        if native_card_ranking and isinstance(st.card_ranking, dict) else None
    )
    ranked_cards: set[str] = set()
    for raw_id in order:
        bounded_id = _card_id(raw_id)
        if bounded_id is None or (not native_card_ranking and bounded_id in ambiguous_seeds):
            continue
        canonical_id = _canon(bounded_id)
        if canonical_id in ranked_cards:
            continue
        rank_i = len(ranked_cards)
        ranked_cards.add(canonical_id)
        c = cards.get(canonical_id)
        if c is not None and c.verdict == "open":
            c.priority = rank_i
            if native_card_ranking or c.foresight_rank is None:
                c.foresight_rank = rank_i
            if (native_card_ranking and not isinstance(ranking_confidence, bool)
                    and isinstance(ranking_confidence, (int, float))
                    and math.isfinite(float(ranking_confidence))
                    and 0.0 <= float(ranking_confidence) <= 1.0):
                c.confidence = float(ranking_confidence)


def _apply_card_operator_overlays(
        st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> None:
    cards = ledger.cards
    _canon = aliases.canon

    # 7) operator-override overlay — FINAL PHASE (docs/23 decision 27: the operator wins regardless of
    #    event arrival order). `card_edited`
    #    overlays the DISPLAY statement only — the join key stays `seed_statement` (docs/23 decision 24).
    # Resolve through `_canon` so a control that named a card before it was merged still lands on the
    # canonical survivor. Bound the stored id first as an additional replay hardening fence.
    for raw_id, edit in (st.card_operator_edits or {}).items():
        bounded_id = _card_id(raw_id)
        c = cards.get(_canon(bounded_id)) if bounded_id is not None else None
        if c is not None and isinstance(edit, dict) and edit.get("statement"):
            c.statement = str(edit["statement"])
            event_seq = edit.get("event_seq")
            if type(event_seq) is int and 0 <= event_seq <= (1 << 31) - 1:
                c.statement_edit_seq = event_seq
    for raw_id, pri in (st.card_priority_pins or {}).items():
        bounded_id = _card_id(raw_id)
        c = cards.get(_canon(bounded_id)) if bounded_id is not None else None
        if c is not None:
            c.pinned = True
            try:
                c.priority = int(pri)
            except (TypeError, ValueError):
                pass
    for raw_id, pin in (st.card_resource_pins or {}).items():
        bounded_id = _card_id(raw_id)
        c = cards.get(_canon(bounded_id)) if bounded_id is not None else None
        if c is not None and isinstance(pin, dict):
            # ``footprint`` participates in the native action digest and MUST remain immutable.
            # Admission/freshness merge this independent override through effective_card_footprint.
            c.resource_pin = {
                **{key: pin[key] for key in ("gpus", "gpu_mem_mib") if key in pin},
                "pinned_by": "operator",
            }


def _apply_card_belief_lineage(
        st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> None:
    """Publish the research-direction facet's OWN identity on every card: `belief_id` + `retry_of`.

    Why this phase exists at all is recorded on the two fields in ``core/cards.py``; the short version
    is that ``id`` names the work item, ``identity.action_digest`` binds the executable action, and
    NEITHER can say "these two work items ask the same question".  A debug retry reuses its parent's
    Idea verbatim and only flips ``operator``, so it is a different action (correctly) and therefore a
    different card (correctly) — and the board had no way left to show it as the same hypothesis.

    Strictly ADDITIVE and derived: this writes only the two new fields.  It never touches a receipt, a
    digest, an action field, evidence, a verdict or a selection blocker, which is why it can run here
    without re-opening any of step 9's fail-closed reasoning.  It runs BEFORE
    ``_apply_card_selection_readiness`` only so the ledger's declared phase order stays "derive every
    projected field, then gate on the final values"; nothing in step 9 reads either field.

    ``retry_of`` resolves the durable parent NODE anchor back to a CARD, which is the join the payload
    has always supported and nothing performed: ``parent_id``/``parent_ids``/``parent_generations`` are
    consumed exclusively as node anchors (``_card_action_has_live_anchors``, ``_card_action_freshness``,
    ``engine/card_reservation.py::_build_parent_snapshot``).  The owner map is keyed on the NODE ROW's
    own ``idea.card_id`` — the same "its own work item" notion step 9 builds for the debug exemption,
    and deliberately NOT ``Card.evidence``: evidence can name a node attached by the legacy
    statement-hash join, which never was that card's work item, and reading it here would invent a
    retry edge out of shared wording.  Canonicalized through ``_canon`` so a merged-away owner resolves
    to its survivor.
    """
    cards = ledger.cards
    _canon = aliases.canon

    # node id -> the card that node was BUILT FOR. `Node.idea` is a required field, but a hostile or
    # future log is folded through the same code path, so read it defensively rather than trusting it.
    owner_card_by_node: dict[int, str] = {}
    for node in st.nodes.values():
        idea = getattr(node, "idea", None)
        owner_card_id = getattr(idea, "card_id", None) if idea is not None else None
        if isinstance(owner_card_id, str) and owner_card_id:
            owner_card_by_node[node.id] = _canon(owner_card_id)

    for cid, c in cards.items():
        seed = (c.seed_statement or "").strip()
        # The FULL sha256 statement digest, never the short display `hypothesis_id` — see the field.
        c.belief_id = hypothesis_statement_digest(seed) if seed else None
        c.retry_of = None
        # Only `debug` is a retry. `improve`/`merge` also name parent nodes, but they propose a NEW
        # point in the space: linking those would claim every child is a re-run of its parent's
        # question, which is the opposite of what the operator needs to see.
        if c.operator != "debug":
            continue
        parents = list(c.parent_ids or [])
        if c.parent_id is not None and not parents:
            parents = [c.parent_id]
        # Exactly one anchor, matching the shape `_card_action_has_live_anchors` admits for `debug`.
        # A two-parent "debug" is malformed, and guessing which half it retries would be a fabrication.
        if len(parents) != 1:
            continue
        owner = owner_card_by_node.get(parents[0])
        # A self-edge is not a retry (and is what a walked chain would need a cycle guard for). It is
        # unreachable today — a card's own work item is its CHILD node, never the parent it debugs —
        # so this is a fence against a future/corrupt log, not a live case.
        if owner is None or owner == _canon(cid) or owner not in cards:
            continue
        c.retry_of = owner


def _apply_card_actionable(ledger: _CardLedger) -> None:
    cards = ledger.cards

    # 8) LAYER-1c exclusion seam — derive `actionable` from the FINAL status/verdict (after every
    #    override). This compatibility flag means only "not administratively dead" for the board:
    #    running/evaluated cards intentionally remain True. It MUST NOT be consumed as proof of
    #    executability; receipt-backed `selection_ready` below is the active queue seam.
    for c in cards.values():
        c.actionable = c.status not in ("dropped", "gated") and c.verdict != "abandoned"


def _apply_card_selection_readiness(st: RunState, ledger: _CardLedger, aliases: _CardAliases,
                                    building_card_ids: set[str]) -> None:
    cards = ledger.cards
    card_origins = ledger.card_origins
    card_registrations = ledger.card_registrations
    action_owners = ledger.action_owners
    _canon = aliases.canon

    # Step 9 fails closed at the executable-action boundary: a selectable Card must be exactly one
    # immutable work item with a durable `card_added` ownership receipt. The native writer supplies it;
    # legacy hash joins, unbound card_added rows, and node-only card ids remain visible but can never
    # become selection-ready.
    breedable_card_parent_ids = {node.id for node in st.breedable_nodes()}
    debuggable_leaf_children = _card_debug_leaf_children(st)
    debuggable_leaf_candidate_ids = _card_debuggable_leaf_candidate_ids(st)
    debuggable_card_parent_ids = _card_debuggable_leaf_ids(
        st, candidate_ids=debuggable_leaf_candidate_ids,
        leaf_children=debuggable_leaf_children)
    # The nodes each Card OWNS, keyed by the CANONICAL card id. `Card.evidence` alone is not enough
    # for the debug exemption below: evidence can name a node whose OWN row never claimed this Card.
    # Intersecting the two makes "its own work item" provable from the node row itself.
    #
    # Be precise about what that buys, because 5620d11f's commit message got it wrong and the next
    # reader will otherwise trust it: this does NOT stop a `card_merged` alias's node from laundering
    # the exemption. This map is keyed by `_canon(...)`, so an alias's node IS in the canonical
    # Card's own-work-item set by construction. What keeps a merged chain closed is the blocker pair
    # it earns anyway — `merged_work_items` (any surviving work-item alias) and usually
    # `action_owner_ambiguous` (>1 action owner) — both of which are unconditional.
    # The intersection's real protection is narrower and worth keeping on its own terms: the
    # legacy STATEMENT-HASH join (step 2 above) attaches a node to a Card by hypothesis wording when
    # the node names no `card_id` at all. Such a node is evidence but was never this Card's work
    # item, and without the intersection it would exempt itself from its own parent's leaf test.
    # (`identity_not_native` blocks those Cards today; that is a second rule, not this one.)
    own_work_items_by_card: dict[str, set[int]] = {}
    for node in st.nodes.values():
        owner_card_id = node.idea.card_id
        if isinstance(owner_card_id, str) and owner_card_id:
            own_work_items_by_card.setdefault(_canon(owner_card_id), set()).add(node.id)
    for cid, c in cards.items():
        registration = card_registrations.get(cid, {})
        if (registration.get("count") == 1 and registration.get("valid_count") == 1
                and isinstance(registration.get("digest"), str)):
            c.identity = CardIdentityProvenance(
                kind="native", source="card_added_receipt", durable=True, receipt_valid=True,
                action_digest=registration["digest"],
            )
        else:
            origin = card_origins.get(cid, "unknown")
            legacy = origin in {"hypothesis_shadow", "node_statement_hash"}
            c.identity = CardIdentityProvenance(
                kind="legacy_hash" if legacy else "synthesized_shadow",
                source=origin if origin in {
                    "card_added_unbound", "hypothesis_shadow", "node_statement_hash",
                    "node_card_id", "merge", "unknown",
                } else "unknown",
            )

        owner = action_owners.get(cid, {"count": 0, "sources": set(), "all_complete": False})
        owner_count = min(257, owner["count"])
        owner_sources = owner["sources"]
        if owner_count == 0:
            action_source = "none"
        elif len(owner_sources) == 1:
            action_source = next(iter(owner_sources))
        else:
            action_source = "mixed"

        projected_action = _card_action_from_projection(c)
        projected_digest = (
            transitional_card_action_digest_v1(c.id, c.seed_statement, projected_action)
            if (isinstance(c.identity.action_digest, str)
                and c.identity.action_digest.startswith("card-action:v1:"))
            else card_action_digest(c.id, c.seed_statement, projected_action)
        )
        action_complete = bool(
            owner_count == 1
            and owner["all_complete"]
            and c.identity.kind == "native"
            and projected_digest == c.identity.action_digest
            and _card_action_has_live_anchors(
                c, breedable_card_parent_ids, debuggable_card_parent_ids,
                debuggable_leaf_candidate_ids=debuggable_leaf_candidate_ids,
                debuggable_leaf_children=debuggable_leaf_children,
                own_work_item_ids=(
                    set(c.evidence) & own_work_items_by_card.get(cid, set())),
            )
        )
        freshness = _card_action_freshness(st, c)

        work_states: set[str] = set()
        if cid in building_card_ids:
            # status='building' is a display lane, not a queue exclusion proof. Carry the exact marker
            # link into selection provenance so a future consumer fails closed on this in-flight owner.
            work_states.add("in_flight")
        for node_id in c.evidence:
            node = st.nodes.get(node_id)
            if node is None:
                work_states.add("unknown")
            elif (node.status is NodeStatus.pending and not node.tombstoned
                  and node.id not in st.aborted_nodes):
                work_states.add("in_flight")
            else:
                work_states.add("terminal")
        if not work_states:
            owner_state = "none"
        elif len(work_states) == 1:
            owner_state = next(iter(work_states))
        elif "unknown" in work_states:
            owner_state = "unknown"
        else:
            owner_state = "mixed"
        c.selection_provenance = CardSelectionProvenance(
            action_source=action_source,
            action_owner_count=owner_count,
            action_complete=action_complete,
            freshness=freshness,
            owner_state=owner_state,
        )

        blockers: list[str] = []
        if c.identity.kind != "native":
            blockers.append("identity_not_native")
        if owner_count == 0:
            blockers.append("action_owner_missing")
        elif owner_count > 1:
            blockers.append("action_owner_ambiguous")
        if owner_count == 1 and not action_complete:
            blockers.append("action_receipt_incomplete")
        if freshness == "unknown":
            blockers.append("freshness_unknown")
        elif freshness == "stale":
            blockers.append("freshness_stale")
        if owner_state in {"in_flight", "mixed"}:
            blockers.append("work_in_flight")
        if owner_state in {"terminal", "mixed"}:
            blockers.append("work_terminal")
        if owner_state == "unknown":
            blockers.append("work_owner_unknown")
        if c.status in {"dropped", "gated"} or c.verdict == "abandoned":
            blockers.append("card_terminal")
        work_item_aliases = [
            alias_id for alias_id in c.aliases
            if not c.seed_statement or alias_id != hypothesis_id(c.seed_statement)
        ]
        if work_item_aliases:
            blockers.append("merged_work_items")
        c.selection_blockers = blockers
        c.selection_ready = not blockers


def _publish_visible_cards(
        st: RunState, ledger: _CardLedger, control_ids: dict[str, set[str]]) -> None:
    cards = ledger.cards

    # A legacy/pure-belief row DELETED by the operator (`hypothesis_updated status=deleted`) is removed
    # entirely; every Card shadow resolved through that compatibility identity must vanish with it. Until
    # a card-native delete exists, reuse `hypotheses_deleted`; `control_ids` maps native work-item ids back
    # to any statement-hash control identities instead of assuming Card id == hypothesis id.
    st.cards = {
        cid: card for cid, card in cards.items()
        if not any(control_id in st.hypotheses_deleted
                   for control_id in control_ids.get(cid, {cid}))
    }


def derive_cards(st: RunState) -> None:
    """Build the derived Card ledger from native receipts and compatibility shadows.

    Cards do not directly choose the metric champion, but the active Card queue consumes receipt-backed
    ``selection_ready`` rows to create candidates. A card is seeded from ``card_added`` events and nodes whose
    `idea.hypothesis` is set (linked by `idea.card_id` when present, else the statement hash — the same
    fallback join `_derive_hypotheses` uses), it accretes the substance on Idea/Node. The `verdict`
    (supported/tested/...) is computed by the SHARED `_evidence_verdict` helper so it is byte-identical
    to the hash-joined hypothesis; a separate lifecycle `status` lane (proposed/running/gated/evaluated/
    dropped) is derived from node outcomes. Bounded enrichment, ranking, operator overlays and lifecycle
    outcomes are applied before selection readiness is derived. Internal order mirrors the hypothesis
    projection where the contracts overlap:
    seed+link -> merge-union -> record-setters once -> shared verdict helper -> drop overrides ->
    operator-overlay (reserved, empty in L1) -> status.

    The numbered phases below are the SAME sequence this function ran inline until doc 25 EV-01 split
    them out, in the same order, and the order is load-bearing: ``_card_identity_map`` must see the
    whole log before any Card exists, the merge fold must run before verdicts (evidence is unioned),
    the operator overlay must run after enrichment and ranking (docs/23 decision 27), and
    ``actionable`` / ``selection_ready`` read the FINAL status. ``_apply_card_belief_lineage`` is the
    one phase with NO ordering constraint of its own — it writes only the two derived research-direction
    identities (``belief_id``/``retry_of``) and no later phase reads them — so it sits where the
    sequence stays readable as "derive every projected field, then gate on the final values".
    Each phase is a pure function of the
    folded ``RunState`` plus the explicit tables threaded through it — nothing here reads an Event,
    a clock or the outside world (engine invariant 5).
    """
    ledger = _CardLedger()
    identity = _card_identity_map(st)
    _seed_cards_from_receipts(st, identity, ledger)
    _link_cards_to_nodes(st, identity, ledger)
    aliases = _card_merge_aliases(st, identity)
    control_ids = _card_control_ids(identity, ledger)
    control_ids = _fold_merged_cards(identity, ledger, aliases, control_ids)
    _apply_card_verdicts(st, ledger, control_ids)
    dropped = _apply_card_drops(st, ledger, aliases)
    building_card_ids = _card_building_ids(st, ledger, aliases)
    _apply_card_status(st, ledger, dropped, building_card_ids)
    _apply_card_enrichment(st, ledger, aliases)
    _apply_card_ranking(st, identity, ledger, aliases)
    _apply_card_operator_overlays(st, ledger, aliases)
    _apply_card_belief_lineage(st, ledger, aliases)
    _apply_card_actionable(ledger)
    _apply_card_selection_readiness(st, ledger, aliases, building_card_ids)
    _publish_visible_cards(st, ledger, control_ids)
