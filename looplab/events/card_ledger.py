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
                     CARD_CHILD_LIMIT, CARD_CONCEPT_TAG_LIMIT, CARD_LINEAGE_MAX_DEPTH,
                     card_child_rollup, card_kind_of,
                     CARD_IDEA_CONCEPT_FIELDS,
                     CARD_STATEMENT_MAX_CHARS,
                     INHERITABLE_CONCEPT_PROVENANCE as _INHERITABLE_CONCEPT_PROVENANCE,
                     NODE_CONCEPT_PROVENANCE_UNTRUSTED,
                     Card, CardConceptSource, CardIdentityProvenance, CardSelectionProvenance,
                     Node, NodeStatus, RunState, card_action_digest,
                     card_ownership_receipt, card_score_fence_state,
                     coerce_node_id as _coerce_node_id,
                     is_unevaluated_speculative_discard,
                     legacy_card_ownership_receipt_v1,
                     transitional_card_action_digest_v1,
                     transitional_card_ownership_receipt_v1,
                     hypothesis_id, hypothesis_statement_digest,
                     idea_proposal_digest,
                     node_counts_toward_card_budget,
                     normalize_extra_metrics, normalize_researcher_footprint,
                     normalize_steering_context,
                     surviving_work_item_aliases,
                     valid_card_action_digest, valid_researcher_footprint)

# A module-private "key absent" marker for ``dict.get``, so an absent receipt stays distinguishable
# from a stored ``None``. ``replay._MISSING`` is the same idea for the fold's handlers; neither
# sentinel ever crosses a module boundary, so these are two private markers rather than one rule
# spelled twice.
_MISSING = object()


_CARD_REPLAY_ID_MAX = 256
_CARD_REPLAY_STATEMENT_MAX = CARD_STATEMENT_MAX_CHARS
_CARD_REPLAY_SOURCE_MAX = 64
_CARD_REPLAY_RATIONALE_MAX = 400
_CARD_REPLAY_ACTION_MAP_MAX = 64
_CARD_REPLAY_ACTION_LIST_MAX = 64
_CARD_REPLAY_MERGE_ALIASES_MAX = 256
_CARD_REPLAY_NODE_ID_MAX = (1 << 31) - 1
# A field-level projection cache, not the audit log. Every row has already crossed the closed,
# independently bounded ``card_enriched`` receipt boundary; full history remains in events.jsonl.
CARD_ENRICHMENT_JOURNAL_MAX = 4_096


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

    # PART V (B): the DELTA half of the same proposal-time envelope. A `delta` proposal states a
    # CHANGE against an inheritance base — the run base at a root, else the union of its parents'
    # effective memberships — and that base only exists in FOLDED state, so the resolution happens in
    # `replay.py::_materialize_concept_deltas` over the whole DAG, never here and never at mint time.
    # What the card row keeps is therefore an audit copy, not a membership: no Card field is derived
    # from it, `_card_added_snapshot` leaves `concept_source.membership_present` False for it, and the
    # EXECUTABLE copy travels on the claimed node's own Idea. It is decoded here for one reason — so
    # replay stops reading these keys as unknown future ACTION members, which is what made a
    # delta-mode Card permanently unselectable and cost the delta lane its concepts entirely.
    # Bounded but unflagged, deliberately: the `_concept_tags_*` flags exist because `concepts`
    # becomes `Card.concept_tags`, where a silently-truncated list would read as an exact membership.
    # Nothing reads these, so there is no completeness claim for a flag to qualify.
    raw_mode = value.get("concept_mode")
    if isinstance(raw_mode, str):
        out["concept_mode"] = raw_mode[:80]
    for delta_field in ("concepts_added", "concepts_removed"):
        if isinstance(value.get(delta_field), list):
            out[delta_field] = bounded_raw_concept_values(value[delta_field])[0]

    if isinstance(value.get("parent_ids"), list):
        out["parent_ids"] = _bounded_card_parent_ids(value["parent_ids"])
    parent_id = _card_replay_node_id(value.get("parent_id"))
    if parent_id is not None:
        out["parent_id"] = parent_id
    if record_unknown_fields:
        known_fields = {
            "operator", "params", "space", "eval_profile", "eval_timeout",
            "concept_tags", *CARD_IDEA_CONCEPT_FIELDS,
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
    # THE RESEARCH-LINEAGE EDGE, and it has to be named HERE or it does not exist. This function
    # rebuilds the replay row from an ALLOW-LIST — `RunState` is deep-copied on every incremental
    # snapshot, so `_on_card_added` must never retain `Event.data` — and a key it does not name is
    # gone before any reader sees it. `_card_added_snapshot` decoded `parent_card_id` faithfully
    # from a dict that had already been stripped of it, so the decoder worked and the field was
    # dead: measured on `runs/e5small-dr-unified-v5`, the durable row named a direction that WAS on
    # the board and the folded child's parent was None with every direction childless.
    # NOT a node id — this is a CARD id, so it takes the same string bound as a card's own id
    # rather than `_card_replay_node_id` one line above it.
    parent_card_id = _card_id(d.get("parent_card_id"))
    if parent_card_id is not None:
        rec["parent_card_id"] = parent_card_id
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


def _sota_eligible(n: Node) -> bool:
    """May this node's metric take part in the run's SOTA at all?

    ONE spelling, because `_record_setter_ids` and `_record_establisher_id` both answer "which node
    is first" and the second one's own docstring says why that matters: "a second reading of 'which
    node is first' is how the two come to disagree about it" — and then the predicate was written
    out twice anyway. The moment a clause is added to one (this repo already maintains populations a
    SOTA rule plausibly grows into — `metric_salvage.unreliable_metric_ids`, the trust gate's
    `flagged_node_ids`), `record_establisher` names a node no longer in `record_setters`, NO member
    is excluded by `_evidence_verdict`'s `n.id != record_establisher` test, and the "a record set
    over nothing is not support" rung silently reverts to calling the opening hypothesis of every
    run `supported` with `best_delta=None` — with nothing red.

    §6.3: a deleted node must not set the board's SOTA.
    """
    return (n.status is NodeStatus.evaluated and n.feasible and n.metric is not None
            and not n.tombstoned)


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
        if _sota_eligible(n):
            if running is None or better(n.metric, running):
                setters.add(n.id)                   # first node ESTABLISHES the SOTA, or a later node
                running = n.metric                  # BEATS the standing record — either is a real advance
    return setters


def _record_establisher_id(nodes: dict[int, Node]) -> int | None:
    """The ONE node in `_record_setter_ids` that beat nothing — the run's first SOTA, or None.

    `_record_setter_ids` folds two different events into one set: a node that ESTABLISHES the first
    record (`running is None`) and a node that BEATS a standing one. Only the second is evidence
    that anything improved, and this names the first so `_evidence_verdict` can tell them apart.

    Derived HERE rather than proxied by "does the node have a feasible parent", which is what the
    first cut of that distinction used. The two agree only on a run whose lineage is a chain: a ROOT
    node that beats a standing sibling record has no parent and is a genuine advance, and under
    card-driven selection most proposals ARE root drafts, so the proxy told the Researcher its best
    experiment had improved on nothing. Same loop as `_record_setter_ids` and the SAME guard object
    (`_sota_eligible`) rather than a retyped copy of it, because a second reading of "which node is
    first" is how the two come to disagree about it — and a docstring saying so beside a duplicated
    predicate is not what stops that. Takes no
    `direction`: which node is FIRST is a fact about creation order, and the comparison that needs a
    direction is the one this node is defined by not having made.
    """
    for n in sorted(nodes.values(), key=lambda x: x.id):
        if _sota_eligible(n):
            return n.id
    return None


def _evidence_verdict(evidence_ids: Iterable[int], nodes: dict[int, Node], direction: str,
                      record_setters: set[int], is_abandoned: bool,
                      *, record_establisher: int | None,
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
        # A RECORD SET OVER NOTHING IS NOT SUPPORT, and the ESTABLISHER is the whole of that
        # distinction. `record_setters` deliberately includes the node that establishes the first
        # SOTA — which on every run is node 0, by being the only node — so this clause used to
        # declare the OPENING hypothesis of every run `supported` with `best_delta=None`, i.e.
        # borne out against nothing at all.
        #
        # It is not a cosmetic wrong word. `verdict` is what the proposal board shows the
        # Researcher, and a model reading "supported" either believes it and stops testing the
        # question, or distrusts the whole board. Measured live on `runs/e5small-dr-unified-v5`:
        # card-0 read `supported` on node 0 (metric 0.693, no parents, one node in the run), the
        # Researcher wrote "…but wait, the card says NODES=[0] and verdict=supported … that's odd"
        # in its own trace, and spent the next proposal re-implementing what the board claimed was
        # already done — card-1, a near-duplicate of card-0.
        #
        # The sticky clause keeps its real job: a node that BEAT a standing record stays supported
        # after something overtakes it, which is the board bug its comment describes. What it no
        # longer does is mint a verdict where no comparison exists. Such a card lands on `tested`
        # ("evaluated without improvement"), which is exactly true of a first measurement.
        #
        # The test is `is not the establisher`, and it was `base is not None` — a PARENT — for one
        # day. Those two agree only on a run whose lineage is a chain. A ROOT node that beats a
        # standing sibling record has no parent and IS a genuine advance, and under card-driven
        # selection most proposals are root drafts: with the parent proxy, a run whose best
        # experiment was a fresh draft read `tested`, i.e. the board told the Researcher its best
        # result had improved on nothing. That is the same class of board lie in the other
        # direction, and this rung exists to remove it, not to swap it.
        if n.id in record_setters and n.id != record_establisher:
            supported = True
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
    kind: Literal["card_added", "card_enriched", "hypothesis_added"], *, present: bool,
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


def _bounded_card_ref(value) -> str | None:
    """One ENRICHMENT ref as a foreign key, or None — a memo/lesson/claim id, not a card id.

    RESTORED to its original contract on 2026-08-27 after a card-lineage change narrowed it for a
    new caller: the bound went 400 -> 256, and refusing a padded value (`value != value.strip()`)
    became silently STRIPPING one. This helper folds `research_origin`, `lesson_refs` and
    `claim_refs` on logs already on disk, so both edits changed replay OUTPUT for rows nobody was
    editing — a legacy ref of 257-400 chars started folding to `None` and vanishing, and a
    whitespace-padded ref that the rule deliberately REFUSED began resolving to a stripped id, which
    is the treat-display-edits-as-identity failure the card-id rules elsewhere warn about. (Only
    LEGACY rows: a `modern` row must additionally be a `sha256:` digest ref, far under either bound.)

    The card-id shape it was narrowed for is `_card_id`, which already spells exactly that rule; the
    `parent_card_id` edge calls it directly.
    """
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 400 or not value.isprintable()):
        return None
    return value


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
    # The RESEARCH-LINEAGE edge, decoded at the top level exactly like `steering_context` above and
    # for the same reason: it is not an executable member. It deliberately does NOT set `owns_action`
    # — naming the direction you serve is not owning an action, and if it counted as one a pure
    # research direction that happened to be filed under a broader one would become an "action owner"
    # and its `action_owner_missing` blocker would silently turn into `action_receipt_incomplete`.
    # The edge is validated for SHAPE here and for legality (self-edge, cycle, unknown target,
    # merged-away target) in `_apply_card_lineage`, which is the only place that can see every card.
    parent_card_id = _card_id(d.get("parent_card_id"))
    if parent_card_id is not None:
        snapshot["parent_card_id"] = parent_card_id
    return snapshot, owns_action


_CARD_ADDED_ACTION_FIELDS = frozenset({
    "operator", "params", "space", "eval_profile", "eval_timeout", "concept_tags",
    # The whole concept envelope, from the ONE tuple its writer also emits (`core/cards.py`). Listing
    # a subset here is the bug this constant exists to make impossible: `concepts` alone admitted a
    # FULL membership and left every `delta` proposal's Card reading as a lossy future schema.
    *CARD_IDEA_CONCEPT_FIELDS,
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

    HISTORICAL SINCE 2026-08-13 (F5), AND KEPT ON PURPOSE — read this before deleting it. The
    PRODUCER is gone: `search/policy.py::debug_action` and the Card lane's forced debug prefix were
    removed with the Debug node, so no run started after that date can author a `debug` Card. This
    reader and its two siblings below stay because `fold` must keep answering correctly about the
    logs that ALREADY EXIST — every preserved run under `runs/` with a `debug` Card folds through
    exactly this map, and a replay that suddenly disagrees with the run's own recorded state is a
    reproducibility break, not a cleanup.
    ``search/card_selection.py::_live_card_action`` is the other half and it moved the OTHER way: a
    historical `debug` Card now falls through to not-live there, so it can never be claimed again.
    That asymmetry is the intended one — replay reports what happened, selection refuses to repeat
    it. Deleting these readers while leaving the folded shape they interpret is the silent-breakage
    direction: the fold would stop distinguishing a `debug` Card's own work item from any other
    child, and the L3 budget and `selection_ready` both key on that (CLAUDE.md invariant 1 records
    the measured flip: `{budget 2, leafs [2,3], later Card ready}` vs `{budget 3, leafs [3], not
    ready}`).

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
    such a parent, and ``card_selection.eligible_cards`` refuses every ready debug Card at
    ``_live_card_action``'s default branch before it can be claimed, so it fails closed at the claim
    instead.  That recheck used to be against the live ``debug_action``; F5 deleted it, and a
    historical ``debug`` Card is simply never live — a strictly stronger refusal than the one this
    paragraph was written for, reached by the default branch rather than by a special case.
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
    A known-dead anchor or a changed attempt is ``stale`` even when another legacy fence is missing,
    keeping the future queue fail closed while old card shadows remain readable.

    The two halves are independent: ``parent_state`` over the action's own parents, ``score_state``
    over the node it was scored against.  With an incumbent the score half is an ANCHOR liveness
    question and nothing more — a merely SUPERSEDED champion is not stale.  ``card_score_fence_state``
    owns that rule (and the empty-authority branch's deliberately different one), so the fold and the
    Layer-5 recheck in ``search/card_selection.py`` cannot drift apart; read its docstring before
    changing either.
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

    scored_node = (
        None if card.scored_against is None else st.nodes.get(card.scored_against)
    )
    score_state = card_score_fence_state(
        card.scored_against,
        card.scored_against_generation,
        card.scored_against_empty,
        anchor_live=(
            scored_node is not None
            and not scored_node.tombstoned
            and card.scored_against not in st.aborted_nodes
        ),
        anchor_attempt=None if scored_node is None else scored_node.attempt,
    )

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
    identity_alias: dict[str, str]
    merge_edges: dict[str, dict[str, int | None]]

    def canon(self, x: str) -> str:                 # resolve alias chains a->b->c, cycle-safe
        seen: set[str] = set()
        while x in self.alias and x not in seen:
            seen.add(x)
            x = self.alias[x]
        return x

    def canon_at(self, x: str, event_index: int | None) -> str:
        """Resolve only explicit merge edges durable at ``event_index``.

        Identity bridges are timeless because they are derived from the materialized Card set. A
        legacy receipt with no trusted physical index retains historical full-canonical behavior.
        """
        if event_index is None:
            return self.canon(x)
        seen: set[str] = set()
        while x not in seen:
            seen.add(x)
            active = [
                target for target, index in self.merge_edges.get(x, {}).items()
                if index is None or index <= event_index
            ]
            target = min(active) if active else self.identity_alias.get(x)
            if target is None:
                break
            x = target
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
            # THE QUESTION'S OWN CONCEPTS, on the path where `snapshot` is empty by construction.
            # `_card_added_snapshot` runs only for a NATIVE row, so a question registered through
            # `hypothesis_added` reached this constructor with no membership at all — measured on
            # `runs/e5small-dr-unified-v5`, all five questions carried `concept_tags=[]` while the
            # run's one experiment carried four, leaving the concept hierarchy and the question
            # board as disjoint taxonomies over one run.
            #
            # The receipt is `hypothesis_added` and deliberately not `card_added`: the tags ARE
            # authored, but by a memo rather than by a card mint, and there is no ownership receipt
            # or action digest behind them. Absent tags leave BOTH the list and the source alone, so
            # every log on disk folds byte-identically and "nobody said" stays distinguishable from
            # "said none".
            question_concepts = d.get("concepts") if not native_row else None
            question_source = (
                {"concept_tags": list(question_concepts),
                 "concept_source": _proposal_card_concept_source(
                     "hypothesis_added", present=True)}
                if isinstance(question_concepts, list) and question_concepts else {}
            )
            cards[cid] = Card(
                id=cid, statement=stmt, seed_statement=stmt,
                source=str(d.get("source") or "human"),   # mirror _derive_hypotheses' default
                rationale=str(d.get("rationale", ""))[:400], created_at_node=at_node,
                **question_source,
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
                     # THE DIRECTION EDGE THE RESEARCHER AUTHORED, on the path that has no
                     # `card_added` receipt to decode it from. `Idea.parent_card_id` rides
                     # `node_created` for free, and until this line the board dropped it on every
                     # such path — `card_driven_selection=False` (which is the LEGACY snapshot
                     # default, i.e. every resumed pre-flag run) and `inject_node`. The edge was
                     # written durably and then silently lost by the only reader that renders it.
                     parent_card_id=(n.idea.parent_card_id or None),
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
            # Part of the same atomic action block: a thin `card_added` that never carried the edge
            # must still take the one its own first node stated, or the backfill leaves the card
            # substance-complete and lineage-blind.
            c.parent_card_id = c.parent_card_id or (n.idea.parent_card_id or None)
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
    identity_alias: dict[str, str] = {}
    for seed_id, owner in seed_owner.items():
        if seed_id != owner:
            alias[seed_id] = owner
            identity_alias[seed_id] = owner
    identity_bridge_ids = frozenset(alias)
    # Edges written by the merge loop below, kept apart from the hash->native bridge seeding above so
    # a conflict can be resolved WITHOUT changing what a bridge means. See the `min()` note at the
    # write site: last-write-wins there made `fold(perm(events))` differ, breaking invariant 5.
    merge_alias: dict[str, str] = {}
    merge_edges: dict[str, dict[str, int | None]] = {}
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
                    raw_index = d.get("_event_index")
                    edge_index = raw_index if type(raw_index) is int and raw_index >= 0 else None
                    targets = merge_edges.setdefault(resolved_alias, {})
                    if canon not in targets:
                        targets[canon] = edge_index
                    elif edge_index is None or targets[canon] is None:
                        targets[canon] = None
                    else:
                        targets[canon] = min(targets[canon], edge_index)
        except Exception:  # noqa: BLE001 — one bad merge record must not brick the fold
            continue
    return _CardAliases(
        alias=alias, identity_bridge_ids=identity_bridge_ids, merged_stmt=merged_stmt,
        identity_alias=identity_alias, merge_edges=merge_edges)


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


def _card_work_item_ids(st: RunState, ledger: _CardLedger) -> frozenset[str]:
    """Every card id in this log that owns EXECUTABLE work, in every spelling it is reachable by.

    The complement is what ``Card.belief_aliases`` certifies: a pure research belief. Read the
    PRE-merge ledger tables — after ``_fold_merged_cards`` the per-member rows are summed onto the
    canonical and the distinction is gone, which is exactly why the blocker could not make it.

    Five durable ways to own work, so a fold that misses one cannot certify a work item as a belief:
      * a node names the id as its ``idea.card_id``. THIS is the launder vector
        (``own_work_items_by_card`` is keyed canonically), and it is read straight off ``st.nodes``
        rather than off the ledger because a node whose card id was suppressed as conflicted/ambiguous
        materializes no card row at all and would otherwise look belief-clean.
      * an action-owner row (`card_added` with an action block, or a linked node).
      * a `card_added` REGISTRATION, i.e. a native work-item identity of its own — even a thin one
        whose action block never landed.
      * ``action_owned_cards``, the backfilled-from-a-node action block.
      * evidence: nodes already joined to it.
    A ``hypothesis_added`` belief with no node and no receipt hits none of the five.
    """
    work_items: set[str] = set()
    for node in st.nodes.values():
        raw = getattr(node.idea, "card_id", None) if node.idea is not None else None
        if isinstance(raw, str) and raw:
            # Both spellings: `_link_cards_to_nodes` strips, `own_work_items_by_card` does not.
            work_items.add(raw)
            work_items.add(raw.strip())
    work_items.update(cid for cid, row in ledger.action_owners.items() if row["count"] > 0)
    work_items.update(cid for cid, row in ledger.card_registrations.items() if row["count"] > 0)
    work_items.update(ledger.action_owned_cards)
    work_items.update(cid for cid, card in ledger.cards.items() if card.evidence)
    work_items.discard("")
    return frozenset(work_items)


def _fold_merged_cards(st: RunState, identity: _CardIdentity, ledger: _CardLedger,
                       aliases: _CardAliases,
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
        # Answered BEFORE the union below rewrites the per-member tables it reads.
        work_item_ids = _card_work_item_ids(st, ledger)
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
        # …then certify, once, over the FINAL alias set (the bridge loop above appends to it). Sorted
        # by construction, and a pure function of folded state — order-tolerant like every phase here.
        for card in folded.values():
            card.belief_aliases = [
                alias_id for alias_id in card.aliases if alias_id not in work_item_ids]
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


def _apply_unexecuted_discards(st: RunState, ledger: _CardLedger) -> None:
    """Take the node ids that PROVABLY never ran out of ``Card.evidence`` and record them beside it.

    THE DEFECT. A speculative prefetch superseded by the Card freshness gate is terminalized
    ``node_failed(reason="superseded")`` before it ever reaches a sandbox. The Layer-5 refund gives
    the run back its node SLOT (``core/models.py::node_counts_toward_card_budget``) — and nothing gave
    back the IDEA. The discarded node stayed in its Card's ``evidence``, and three separate readers key
    on exactly that list, so one never-executed build retired the hypothesis three times over:

    * ``search/card_selection.py::_strictly_selection_ready`` wants ``not card.evidence``, and the
      fold's own readiness pass derives ``owner_state="terminal"`` from the same list and stamps the
      ``work_terminal`` blocker — so the Card lane can never elect it again;
    * ``RunState.open_research_beliefs`` filters on ``if c.evidence``, so it leaves the claimable
      untested-belief feed the Researcher proposal prompt is built from;
    * ``agents/roles.py::attempted_board_prompt_cards`` admits it (``c.evidence`` is non-empty) and
      renders it under *"each already has an experiment — do NOT propose one of these again as if it
      were new"*, followed by *"A failed experiment is re-attempted by the engine itself, under the
      same card, without being asked"*. Both sentences are FALSE about this card: there is no
      experiment, and nothing re-attempts a superseded prefetch. So the board did not merely forget
      the idea, it instructed the one role that could have re-proposed it not to.

    Measured cost: ``runs/rubertlite-dr-unified-v7`` permanently lost "hard-negative mining" and
    "label smoothing", both deep-research directions, to builds that never ran.

    THE RULE IS THE ONE ALREADY PROVEN, not a new one. ``is_unevaluated_speculative_discard`` is the
    four-fact durable proof the budget refund is built on (both speculative receipts, the creator's
    promised eval-start boundary with no boundary ever appended, the writer's pre-dispatch marker, and
    corroborating execution evidence — zero charged seconds, no ``stage_finished`` row). If that
    predicate holds, the node did not run; if the node did not run, the question it was built to answer
    is UNTESTED, and calling it evidence is the false statement. Deciding it here rather than at each
    reader is what keeps the three lanes from disagreeing about what "tested" means — the same argument
    ``node_counts_toward_card_budget`` makes for living in ``core`` rather than in ``search``.

    THE BOUND IS ONCE PER CARD, and it is the design content. A returned idea that is re-elected,
    re-built and re-superseded burns a Developer call each time, and a supersede is a statement about
    FRESHNESS AT A MOMENT, so nothing about the first one predicts the second. The first discard is
    therefore returned; a card that has accumulated TWO keeps them both in ``evidence`` and retires for
    good, reading ``failed`` with ``status_nodes`` naming them. The argument for that asymmetry: one
    supersede says only that the board moved while this build ran and says nothing about the idea, while
    a second says the same card has now twice failed to be built inside this board's rate of change —
    which IS a durable fact about the card, and the run should stop paying for it. Deterministic and
    order-tolerant because it is a COUNT over the folded node set, never a choice of which discard to
    forgive: at two, none is forgiven. Worst case per card is exactly two Developer builds, and the
    physical ceiling (``search/card_selection.py::refunded_node_reservations``) still caps the run's
    total refunds at one whole operator budget underneath it.

    SUBSUMPTION NEEDS NO SPECIAL CASE, which is why there is none. If something better subsumed the
    idea while its build ran, the returned Card simply loses the next election to the thing that
    subsumed it and sits on the board costing nothing — the election is already the mechanism that
    compares candidates, and re-deciding merit here would be exactly the model-free-but-invented
    judgement docs/36 refuses.

    ``gated`` STAYS UNREACHABLE FROM HERE, and that is why the filter requires the discards to be the
    card's ONLY evidence. ``_apply_card_status``'s ``gated`` branch fires when EVERY evidence node is
    trust-gated/breed-excluded/infeasible; removing a discarded node from a MIXED evidence set could
    turn a set that was not all-excluded into one that is, minting a ``gated`` card — the one lane that
    feeds ``actionable=False`` and the ``card_terminal`` blocker. Requiring the discards to be the whole
    set makes the only status transition this phase can cause ``failed`` -> ``proposed`` (via the empty
    ``ev_nodes`` branch), which is provable rather than argued. It also keeps the change to its subject:
    a Card that also holds a real experiment HAS been tested, and is not what was retired.

    ``discarded_nodes`` is stamped UNCONDITIONALLY, including for the retired two-discard case and for
    cards that keep their evidence — nothing is un-written, and the loss stays visible to an operator
    whichever side of the bound the card falls on.

    Placed after the merge fold (which UNIONS alias evidence, so the count must see the merged set) and
    before verdicts/status/readiness (which are the readers). ``_apply_card_enrichment`` runs later and
    unions this list back in for node ATTRIBUTION — a novelty/footprint signal riding the discarded
    node's Idea still belongs to its card; attribution is not evidence.
    """

    for c in ledger.cards.values():
        discarded = sorted(
            node_id for node_id in c.evidence
            if (node := st.nodes.get(node_id)) is not None
            and is_unevaluated_speculative_discard(st, node)
        )
        c.discarded_nodes = discarded
        if len(discarded) == 1 and set(discarded) == set(c.evidence):
            c.evidence = []


def _apply_card_verdicts(
        st: RunState, ledger: _CardLedger, control_ids: dict[str, set[str]]) -> None:
    cards = ledger.cards

    # 3) record-setters (sticky SOTA advancers) — the SAME pure helper the hypotheses use, so a card's
    #    verdict is byte-identical to its hash-joined hypothesis.
    _record_setters = _record_setter_ids(st.nodes, st.direction)
    # …and the ONE of them that beat nothing. Derived once per fold beside the set it partitions,
    # never per card: the two must be read off the same `st.nodes` or they disagree about which
    # node was first.
    _record_establisher = _record_establisher_id(st.nodes)

    # 4) verdict per card via the SHARED helper (open/testing/supported/tested/abandoned). `is_abandoned`
    #    mirrors the hypothesis: a shadow card keyed by the hypothesis id inherits the abandoned override.
    for c in cards.values():
        c.best_delta, c.verdict, _ = _evidence_verdict(
            c.evidence, st.nodes, st.direction, _record_setters,
            any(control_id in st.hypotheses_abandoned
                for control_id in control_ids.get(c.id, {c.id})),
            record_establisher=_record_establisher)


def _drop_author(receipt: dict) -> str:
    """Who a drop receipt is attributed to, defaulting to the engine.

    ONE spelling, because three readers ask it and they must not drift: the reopen gate's
    "may this be undone", the engine-retirement history it is checked against, and the
    `Card.dropped_by` the board publishes. `dropped_by` is the current key and `by` the legacy
    one; an unattributed receipt reads as the engine's, which is the fail-closed direction for
    every one of the three.
    """
    return str(receipt.get("dropped_by") or receipt.get("by") or "engine")


def _apply_card_drops(st: RunState, ledger: _CardLedger, aliases: _CardAliases) -> dict[str, dict]:
    cards = ledger.cards

    # 5) apply `card_auto_dropped` engine effects and `card_dropped` operator overrides. The card STAYS
    #    visible (like an abandoned hypothesis) — lifecycle `status` shows the 'dropped' lane. Historical
    #    engine-authored `card_dropped` rows are already normalized into the same bounded receipt list.
    dropped: dict[str, dict] = {}
    # EVERY engine-authored drop this card has ever carried, by `_event_index`. `dropped` is
    # LAST-RECEIPT-WINS over BOTH authorities, so it cannot answer "did the engine ever retire this
    # card" — an operator `card_dropped` landing after a `card_auto_dropped` overwrites the entry
    # and the engine's retirement becomes invisible to any reader of `dropped` alone. The reopen
    # gate below is exactly such a reader, so it needs the history rather than the head.
    # A receipt whose index is unusable is recorded as `None` and treated as blocking: an
    # unordered engine retirement cannot be proven to precede or follow anything, and the
    # conservative answer on this gate is the one the surrounding comments already take.
    engine_drop_indices: dict[str, list] = {}
    for d in st.cards_dropped:
        raw_id = d.get("id")
        bounded_id = _card_id(raw_id)
        if bounded_id is None:
            continue
        raw_index = d.get("_event_index")
        drop_index = raw_index if type(raw_index) is int and raw_index >= 0 else None
        # ORDER-DEPENDENT BY DESIGN: `canon_at` makes the drop's resolution depend on the
        # LANDED ORDER of the drop vs the merge receipt — deliberate in-log semantics, but note
        # `hypothesis_merged` is in NON_CARD_SELECTION_BACKGROUND_APPENDABLE (legacy mode), so its
        # byte position vs a concurrent operator `card_dropped` control append is race-determined
        # at write time, and per invariant 1's own question ("does any reader key on its
        # position?") this reader turned that answer from no to yes. `types.py` now records that on
        # the registry itself; the splice-neutrality proof still does not model a racing drop, so
        # any further use of `canon_at` has to re-ask the question there too.
        cid = aliases.canon_at(bounded_id, drop_index)
        if cid:
            dropped[cid] = d
            if _drop_author(d) != "operator":
                engine_drop_indices.setdefault(cid, []).append(drop_index)
    # A REOPEN SUPERSEDES AN EARLIER DROP, and only an earlier one. Resolution is LAST RECEIPT WINS
    # by `_event_index`, so drop / reopen / drop is expressible and a replay of the same log gives
    # the same board every time. Until this shipped, `cards_dropped` accumulated and nothing ever
    # removed an entry: an operator stop was TERMINAL, the card sat visible-but-unactionable in the
    # `dropped` lane, and no event in the vocabulary could put it back. The operator asked for the
    # control by name.
    #
    # The DROP RECEIPT SURVIVES in `st.cards_dropped` — the log is append-only and who stopped the
    # work and why is history the reopened card still owes its reader, which is the same reason
    # `Card.discarded_nodes` keeps nodes that never ran instead of deleting them. What changes is
    # only whether the drop is APPLIED.
    #
    # A reopen with no index cannot claim to be later than anything: `drop_index` is stamped on
    # every receipt by the fold, so a missing one means a hand-written or pre-upgrade row, and the
    # conservative answer is to leave the drop standing rather than let an unordered receipt revive
    # a card the operator stopped.
    for r in st.cards_reopened:
        bounded_id = _card_id(r.get("id"))
        raw_index = r.get("_event_index")
        if bounded_id is None or type(raw_index) is not int or raw_index < 0:
            continue
        cid = aliases.canon_at(bounded_id, raw_index)
        prior = dropped.get(cid) if cid else None
        if prior is None:
            continue
        prior_index = prior.get("_event_index")
        if not (type(prior_index) is int and prior_index < raw_index):
            continue
        # A REOPEN MAY ONLY UNDO AN OPERATOR'S OWN DROP. `st.cards_dropped` holds TWO authorities:
        # the operator's `card_dropped` and the engine's `card_auto_dropped`, folded by one handler
        # into one list — so an unqualified pop let an operator override the engine's own lifecycle
        # retirement. `card_reservation._record_node_less_card` mints a Card and auto-drops it in a
        # single `append_many` precisely so a REJECTED proposal is retained for audit and never
        # live; reopening one put it back on the selectable board. Worse, `_drop_card_once` is
        # idempotent by HISTORY — it refuses to re-plan a drop for a card any drop receipt already
        # names — so the engine could never retire it again: permanently un-droppable by its owner.
        #
        # Fail-closed on an unattributed receipt, which is the same reading the card itself already
        # publishes (`dropped_by` defaults to "engine" three lines below). "operator" is the
        # established spelling of this authority — `engine/resources.py` and `engine/evaluate.py`
        # both gate on exactly it — and `control_validation` stamps it server-side, so it cannot be
        # forged by the payload.
        if _drop_author(prior) != "operator":
            continue
        # …AND THE HEAD RECEIPT IS NOT THE WHOLE AUTHORITY QUESTION. `dropped` is last-wins across
        # both authorities, and `control_validation._precondition_card` deliberately EXCLUDES
        # `EV_CARD_DROPPED` from its terminal-lifecycle refusal so "an operator keeps authority over
        # the DROP itself on a terminal Card". Those two facts compose into a laundering path: the
        # engine appends `card_auto_dropped` for a rejected proposal, the operator appends their own
        # `card_dropped` over it (server-stamped `dropped_by: "operator"`), and the head receipt now
        # reads as theirs — so the check above passes and the `pop` below removes the ENGINE's
        # retirement too. That is precisely the state the comment above calls unrecoverable, since
        # `_drop_card_once` is idempotent by HISTORY and can never re-retire the card.
        # So an engine drop is undone by NOTHING: if any engine-authored receipt precedes this
        # reopen, the drop stands, whoever wrote the most recent row.
        if any(i is None or i < raw_index for i in engine_drop_indices.get(cid, ())):
            continue
        dropped.pop(cid, None)
    for cid, d in dropped.items():
        c = cards.get(cid)
        if c is not None:
            reason = str(d.get("reason", "") or "")[:400]
            c.dropped_reason = reason or None
            c.dropped_by = _drop_author(d)
            # PUBLISH THE GATE'S OWN ANSWER, so the server's refusal and the board's affordance read
            # the fold rather than each re-deriving it — `Card.reopenable` says why that matters.
            # A future reopen's `_event_index` is greater than every receipt already folded (the log
            # is append-only), so "an engine drop exists for this card" and "an engine drop precedes
            # the next reopen" are the same statement, and the loop above already holds it.
            c.reopenable = (_drop_author(d) == "operator"
                            and not engine_drop_indices.get(cid))
    return dropped


def _card_building_ids(st: RunState, ledger: _CardLedger,
                       aliases: _CardAliases) -> dict[str, list[int]]:
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
    #
    # Returns canonical card id -> the marker NODE ids it owns, ascending. It was a bare `set[str]`
    # until the status lane learned to publish its own subject (`Card.status_nodes`): both consumers
    # only ever ask `cid in …`, which a mapping answers identically, and the ids were already in hand
    # here. A `building` card is the ONE lane whose node is not in `Card.evidence` (see the paragraph
    # above — a reservation is not evidence), so without this it was also the one lane that could
    # name nothing at all.
    building_card_nodes: dict[str, list[int]] = {}
    for node_id, marker in st.buildings.items():
        if not isinstance(marker, dict):
            continue
        marker_card_id = _card_id(marker.get("card_id"))
        if marker_card_id is None:
            continue
        canonical_id = _canon(marker_card_id)
        if canonical_id in cards:
            owned = building_card_nodes.setdefault(canonical_id, [])
            if type(node_id) is int and node_id not in owned:
                owned.append(node_id)
    for owned in building_card_nodes.values():
        owned.sort()
    return building_card_nodes


def _apply_card_status(st: RunState, ledger: _CardLedger, dropped: dict[str, dict],
                       building_card_nodes: Mapping[str, list[int]]) -> None:
    cards = ledger.cards

    # 6) lifecycle `status` lane (frozen vocab; DISTINCT from the verdict). Dropped/merged-away wins;
    #    else an explicit node_building.card_id reservation -> building; else a pending node whose
    #    evaluation is PROVABLY not started -> coded, and any other pending node -> running; else
    #    evidence all trust-gated/breed-excluded/infeasible -> gated; else evidence all FAILED (no
    #    experiment reached a result) -> failed; else terminal evidence -> evaluated; no evidence ->
    #    proposed.
    #
    # Every branch also stamps `Card.status_nodes` — the node ids THIS branch read. A status is a
    # RECORD-side statement (docs/36): the operator reads it to decide whether to intervene, so it
    # owes them the subject it is about. Derived here rather than beside `evidence` so it cannot
    # drift from the lane it explains: one branch writes both, in one pass.
    for cid, c in cards.items():
        if cid in dropped or c.merged_into:
            c.status = "dropped"
            c.status_nodes = []
            continue
        ev_nodes = [st.nodes[i] for i in c.evidence if i in st.nodes and not st.nodes[i].tombstoned]
        # THE PENDING SPLIT, and it is the whole of the 2026-08-14 report. `card-2` on
        # `runs/rubertlite-dr-unified-v7` read `running` while node 2 had been BUILT and never
        # dispatched: `speculation_depth: 2` builds ahead ON PURPOSE, so admitted-but-not-started is a
        # state the design produces deliberately and the board had no word for. A pending node is only
        # PROVABLY not active when its creator PROMISED the durable eval-start boundary
        # (`Node.eval_start_boundary`, stamped on `node_created`) and the CURRENT engine owner has not
        # admitted it. The durable `eval_started` sibling deliberately remains true across owners for
        # Layer-5 budget accounting; `eval_activity_started` is the live claim this display needs.
        # Without the promise, silence is not evidence: an old log written before the boundary existed
        # says the same nothing about a node whose sandbox has been training for forty minutes.
        # FAIL CLOSED, and note which way closed points here — an unproven pending node keeps the
        # `running` lane it has always had (3 of the 4 pending cards in the 45-run corpus), because
        # the defect being removed is a claim that work is HAPPENING, and only a proven boundary can
        # withdraw that claim without inventing a second one.
        started_ids: list[int] = []
        built_ids: list[int] = []
        for n in ev_nodes:
            if n.status is not NodeStatus.pending:
                continue
            if (getattr(n, "eval_start_boundary", False) is True
                    and getattr(n, "eval_activity_started", False) is not True):
                built_ids.append(n.id)
            else:
                started_ids.append(n.id)
        if cid in building_card_nodes:
            c.status = "building"
            c.status_nodes = list(building_card_nodes[cid])
        elif not ev_nodes:
            c.status = "proposed"
            c.status_nodes = []
        elif started_ids:
            # A card with one running node and one merely built IS running: work is happening for it.
            c.status = "running"
            c.status_nodes = sorted(started_ids)
        elif built_ids:
            # `coded` — "code exists and is waiting to run". A lane the model, the UI's CARD_COLUMNS
            # table and BOTH engine readers (`search/card_selection.py`'s `{"coded", "running"}` pair)
            # have carried as RESERVED since the board shipped, waiting on precisely this evidence.
            # Occupying it is therefore behaviour-neutral by construction: `coded` is a strict subset
            # of what was `running`, and every consumer already treats the two identically.
            c.status = "coded"
            c.status_nodes = sorted(built_ids)
        elif all((n.id in st.breed_excluded) or (not n.feasible) for n in ev_nodes):
            c.status = "gated"
            c.status_nodes = sorted(n.id for n in ev_nodes)
        elif all(n.status is NodeStatus.failed for n in ev_nodes):
            # THE TERMINAL TWIN of the pending split. `evaluated` says "evidence has reached a
            # verdict"; for a card whose every experiment FAILED, nothing was measured and the card's
            # own `verdict` already says so ("open" — `_evidence_verdict`'s all-failed branch), so the
            # two lanes contradicted each other. 21 of the 128 terminal cards in the 45-run corpus
            # were in this state, and 3 of them are the strongest form: a speculative build the
            # Layer-5 refund PROVED never ran (`is_unevaluated_speculative_discard`) — the same
            # false claim as card-2's, from the other end of the lifecycle.
            #
            # ONE lane covers both the crash and the discard on purpose. They share the only statement
            # that is true of both ("the experiments ended without a result"); splitting them would
            # mint a second word for a distinction the card's attempts pane, `error_reason` and the
            # refund receipt already carry. It is placed AFTER `gated` so it can only ever take cards
            # that read `evaluated` today — `gated` is the lane that feeds `actionable=False` and the
            # `card_terminal` selection blocker, and no card may cross INTO or OUT of it here.
            c.status = "failed"
            c.status_nodes = sorted(n.id for n in ev_nodes)
        else:
            c.status = "evaluated"
            c.status_nodes = sorted(n.id for n in ev_nodes)


def _card_enrichment_order(row: Mapping) -> tuple[int, int]:
    return (
        row.get("_seq") if type(row.get("_seq")) is int else -1,
        row.get("_event_index") if type(row.get("_event_index")) is int else -1,
    )


_CARD_ENRICHMENT_IDENTITY = frozenset({
    "id", "node_id", "generation", "proposal_ref", "_seq", "_event_index", "_omitted",
})


def _card_enrichment_fields(row: Mapping) -> list[str]:
    """Every semantic field this row carries, in row order.

    Handler-time compaction emits ONE field per row, so this is normally a single name. A journal
    written by an older reader carries them together on one row, and that shape has to keep deriving
    the same Card — the derive seam is additive/back-compatible even though the fold now compacts.
    """
    return [key for key in row
            if key not in _CARD_ENRICHMENT_IDENTITY and not key.startswith("_concept_tags_")]


def _card_enrichment_field(row: Mapping) -> str | None:
    fields = _card_enrichment_fields(row)
    return fields[0] if len(fields) == 1 else None


def _recompact_card_enrichment(
    st: RunState,
    ledger: _CardLedger,
    aliases: _CardAliases,
    node_to_card: Mapping[int, str],
    cap_omissions: Mapping[tuple, int] | None,
) -> set[str]:
    """Collapse raw-id/fence windows at the final canonical Card boundary.

    Handler-time compaction cannot use aliases or Node lifecycles: both can arrive in a later suffix.
    Finalization runs on an isolated FoldCursor snapshot, so it can choose the newest candidate that is
    applicable now while the cursor retains every admitted predecessor for a later lifecycle suffix.
    Hard-cap loss is aggregated by canonical Card/field and surfaced on the surviving window.
    """
    cards = ledger.cards
    winners: dict[tuple[str, str], tuple[bool, dict]] = {}
    omitted: dict[tuple[str, str], int] = {}

    def canonical(raw_id) -> str | None:
        bounded = _card_id(raw_id)
        if bounded is None:
            return None
        try:
            return aliases.canon(bounded)
        except Exception:  # noqa: BLE001 - malformed alias graphs remain non-fatal
            return None

    for source in st.cards_enriched:
        if not isinstance(source, Mapping):
            continue
        card_id = canonical(source.get("id"))
        fields = _card_enrichment_fields(source)
        if card_id is None or card_id not in cards or not fields:
            continue
        modern = {"node_id", "generation", "proposal_ref"} <= set(source)
        applicable = not modern
        if modern:
            subject = _card_sidecar_subject(st, dict(source, id=card_id), node_to_card)
            applicable = subject is not None and subject == card_id
        raw_omitted = source.get("_omitted")
        # One candidate PER FIELD. A handler-compacted row has exactly one and this is a plain
        # rename; a legacy multi-field row splits here, which is what keeps its Card derivation
        # byte-equivalent instead of dropping the whole row as ambiguous.
        for field in fields:
            row = {key: source[key] for key in source
                   if key in _CARD_ENRICHMENT_IDENTITY or key.startswith("_concept_tags_")}
            row["id"] = card_id
            row[field] = source[field]
            group = (card_id, field)
            previous = winners.get(group)
            # Applicability outranks recency. Within one applicability class, envelope LWW applies.
            if (previous is None or (applicable and not previous[0])
                    or (applicable == previous[0]
                        and _card_enrichment_order(row) >= _card_enrichment_order(previous[1]))):
                winners[group] = (applicable, row)
            if type(raw_omitted) is int and raw_omitted > 0:
                omitted[group] = min((1 << 31) - 1, omitted.get(group, 0) + raw_omitted)

    for raw_key, count in (cap_omissions or {}).items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 5 or type(count) is not int or count <= 0:
            continue
        card_id = canonical(raw_key[0])
        field = raw_key[4]
        if card_id is None or card_id not in cards or not isinstance(field, str):
            continue
        group = (card_id, field)
        omitted[group] = min((1 << 31) - 1, omitted.get(group, 0) + count)

    compacted: list[dict] = []
    for group, (_applicable, row) in winners.items():
        loss = omitted.get(group, 0)
        if loss:
            row["_omitted"] = loss
        else:
            row.pop("_omitted", None)
        compacted.append(row)
    st.cards_enriched = sorted(compacted, key=lambda row: (
        row.get("id", ""), _card_enrichment_field(row) or "", *_card_enrichment_order(row)))
    return {card_id for (card_id, _field), count in omitted.items() if count > 0}


def _apply_card_applied_params(st: RunState, ledger: _CardLedger) -> None:
    """Publish, beside every card's PROPOSED `params`, the coordinates its experiment actually ran at.

    `Card.params` is receipt-bound and stays exactly as it was minted — the receipt records what was
    proposed and correcting it would unmake the card's identity. What was missing is the other half.
    Under `params_style: "none"` the engine applies nothing and the Developer realises the idea by
    editing the repo, so a repair that fits a training into memory moves the numbers while the
    proposal stays frozen: 457 comparisons across the corpus, 41 diverged, 18 on nodes that produced
    a metric, and the e5 champion recorded at batch 8192 / accum 2 / 15 epochs ran 512 / 32 / 3.

    THE LATEST EVALUATED EVIDENCE NODE WINS, and its id is published with the numbers. Two rules,
    each refusing a tempting alternative:

      * MERGING several evidence nodes' applied maps would mint a coordinate set no single run ever
        occupied — the same fabrication as reading the proposal, one step subtler.
      * Picking the BEST node would need the run direction and would make the row move when an
        unrelated node scored. "What this card most recently ran at" is a fact about the card; "what
        its best attempt ran at" is a fact about a ranking, and the two must not share a field.

    `effective_params` is NOT used here, deliberately: it falls back to the DECLARATION when no
    applied record exists, which is right for a reader asking "what were this node's numbers" and
    wrong for a field whose entire purpose is to be distinguishable from the declaration. A card
    whose nodes predate the applied record (or never bound a metric) publishes NOTHING and the empty
    map means "not recorded", never "the same as proposed".
    """
    for card in ledger.cards.values():
        card.applied_params = {}
        card.applied_params_node = None
        for node_id in sorted(card.evidence, reverse=True):
            node = st.nodes.get(node_id)
            if node is None or node.status is not NodeStatus.evaluated:
                continue
            provenance = getattr(node, "metric_provenance", None)
            record = provenance.get("applied_params") if isinstance(provenance, dict) else None
            applied = record.get("applied") if isinstance(record, dict) else None
            if not isinstance(applied, dict) or not applied:
                continue
            bounded = normalize_extra_metrics(applied, max_items=64)
            if bounded:
                card.applied_params = bounded
                card.applied_params_node = node_id
            break


def _apply_card_enrichment(
    st: RunState,
    ledger: _CardLedger,
    aliases: _CardAliases,
    cap_omissions: Mapping[tuple, int] | None,
) -> None:
    cards = ledger.cards
    _canon = aliases.canon

    # 6b) LAYER-1b ENRICHMENT — re-home the folded "homeless" signals onto the card + apply explicit
    #     card_enriched deltas. Every source is ALREADY folded (the linking node's Idea, the novelty/
    #     cross-run sidecars) or a main-task card event, so this stays pure/deterministic. Operator
    #     overrides (step 7) run AFTER, so an operator pin always wins over an engine enrichment.
    node_to_card: dict[int, str] = {}
    for cid, c in cards.items():
        # ATTRIBUTION, not evidence — `_apply_unexecuted_discards` already took the proven-never-run
        # ids out of `evidence`, and a novelty/cross-run/footprint sidecar produced during that build
        # still belongs to the card it was built for. Dropping them here would silently un-home those
        # signals as a side effect of returning the idea, which is the opposite of the intent.
        for nid in [*c.evidence, *c.discarded_nodes]:
            node_to_card.setdefault(nid, cid)   # first card claiming a node wins (evidence is per-card)

    incomplete_cards = _recompact_card_enrichment(
        st, ledger, aliases, node_to_card, cap_omissions)
    for card_id in incomplete_cards:
        cards[card_id]._card_enrichment_complete = False

    # Researcher-proposed footprint + research origin ride the linking node's Idea/Node (earliest wins).
    # Same ATTRIBUTION rule as `node_to_card` above: a discarded prefetch's Idea still carries the
    # footprint and the research memo the card was proposed from, and losing the RESEARCH ORIGIN is the
    # sharpest form of the loss being fixed — the retired ideas measured on `rubertlite-dr-unified-v7`
    # came from deep research, and a returned card that no longer names its memo is half a return.
    for c in cards.values():
        for nid in [*c.evidence, *c.discarded_nodes]:
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
    for d in sorted(st.cards_enriched, key=_card_enrichment_order):
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
    Idea verbatim and only flips ``operator``, so it is a different action (correctly) — and the board
    had no way left to show it as the same hypothesis.

    It no longer NEEDS a card of its own to record that action: since
    ``engine/card_reservation.py::_retry_attach_card`` the retry claims the card it retries and the
    node row carries the debug action, so a same-belief retry produces no second row for this phase to
    join.  These derivations stay exactly as they are.  ``belief_id`` is read BY that attach rule (it
    is the "same question?" test), so removing it would silently un-fix the mint; and ``retry_of``
    still has live work — every pre-fix log folds through here unchanged, and a retry whose statement
    was genuinely re-scoped still mints its own card and still earns the edge.

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
                                    building_card_nodes: Mapping[str, list[int]]) -> None:
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
    # it earns anyway — `merged_work_items` and usually `action_owner_ambiguous` (>1 action owner).
    # Both remain unconditional FOR A WORK ITEM, and note the second is not incidental to the first:
    # a node is precisely what makes an alias a work item, and every node linked to a card id records
    # an action owner for that id (`_link_cards_to_nodes`), so a launderable alias always pushes the
    # owner count past one as well. `merged_work_items` is now keyed on `Card.belief_aliases` — the
    # fold's certificate that an alias owns no work at all — so consolidating duplicate BELIEFS into
    # a work item no longer disables it, while every alias that could carry a node still shuts it.
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
        if cid in building_card_nodes:
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
        # Only a surviving WORK-ITEM alias closes the chain. A consolidated pure belief does not —
        # see `surviving_work_item_aliases` for the distinction, why the Card model can state it, and
        # what folding the two together cost on `runs/rubertlite-dr-unified-v6`.
        if surviving_work_item_aliases(c):
            blockers.append("merged_work_items")
        c.selection_blockers = blockers
        c.selection_ready = not blockers


def _apply_card_lineage(ledger: _CardLedger, aliases: _CardAliases) -> None:
    """Publish the DIRECTION -> EXPERIMENT forest: `card_kind`, `parent_card_id`, `child_card_ids`,
    `child_rollup`.

    THE RELATION THIS ADDS, and why the board needed it. Two facts had no way to be said together:
    a Card is one minimal-change hypothesis (that is the whole point of the card/node split), and
    research is done in FAMILIES of them under one broad question. A direction like "distil from a
    stronger teacher" is not a minimal change and can never be made into one, so it was written to
    the board as a row that owns no action — `identity_not_native`, `action_owner_missing` — sitting
    among the work items forever, unbuildable by construction. Measured on
    `runs/e5small-dr-unified-v5`: 5 of 5 rows were directions, 0 were buildable, and the board
    nevertheless read as full. The previous grouping key was `belief_id`, a hash of the seed
    statement TEXT, which put 140 cards of one run into 31 groups with 19 singletons: paraphrase is
    a new belief, so it grouped nothing an operator would call the same question.

    ORDERING. This phase runs AFTER `_apply_card_selection_readiness` — the only hard ordering
    constraint in the whole ledger besides step 9's — because `card_kind` is derived from
    `selection_provenance.action_source`, which that step writes. It runs BEFORE
    `_publish_visible_cards` so the fields are on the rows the wire actually carries. Nothing later
    reads them.

    THE EDGES ALWAYS FORM A FOREST, and that is enforced here rather than promised. Four refusals,
    each of which a hostile, corrupt or merely old log can produce:

      * an edge naming a card that does not exist (a target dropped from a truncated log);
      * a SELF edge, including one that becomes a self edge only after canonicalization — a card
        merged INTO its own declared parent resolves to itself, which is exactly the case a
        "`raw != cid`" check at decode time would miss;
      * an edge that lies ON a cycle — every edge of the cycle, because there is no principled
        way to elect one of three mutually-referring cards as the mistake. A card hanging OFF a
        cycle member keeps its edge: the cycle members become roots, so its chain terminates;
      * a chain already `CARD_LINEAGE_MAX_DEPTH` deep, which becomes its own root rather than
        deepening a tree no consumer is willing to walk.

    A refused edge leaves `parent_card_id` None — the card is a root, which is what it was before
    this phase existed. Refusal is never an exception: one malformed row must not brick the fold.

    THE ROLLUP IS NOT A STATUS, and this is the operator-visible half. Giving a parent its children's
    worst/latest lane would park a broad direction in **Running** for months because one of two
    hundred experiments under it is training. `card_child_rollup` returns COUNTS instead, exact even
    where `child_card_ids` clips at `CARD_CHILD_LIMIT`.
    """
    cards = ledger.cards
    _canon = aliases.canon

    # 1) Kind for every row, and a clean slate for the derived halves. Reset FIRST and
    #    unconditionally: these are derived overlays, and a row that lost its edge this fold must not
    #    keep the one it had last fold (the ledger object is rebuilt per fold today, so this is a
    #    fence rather than a live case — but a stale inverse edge is invisible once written).
    for cid, c in cards.items():
        c.card_kind = card_kind_of(c)
        c.child_card_ids = []
        c.child_rollup = None

    # 2) Resolve every declared edge to a canonical, existing, non-self target.
    declared: dict[str, str] = {}
    for cid, c in cards.items():
        raw = c.parent_card_id
        c.parent_card_id = None
        if not isinstance(raw, str) or not raw:
            continue
        target = _canon(raw)
        if target not in cards or target == _canon(cid):
            continue
        declared[cid] = target

    # 3) Refuse every edge that lies ON a cycle — and ONLY those. The first draft walked up from each
    #    edge's target and refused any edge whose walk came back around, which reads right and is
    #    wrong twice: in a pure 3-cycle EVERY edge comes back around, so all three were refused and
    #    the "keep the rest of the chain" promise was empty; and a perfectly legal card hanging off a
    #    cycle member lost its edge as collateral, because its walk cannot terminate either. Peeling
    #    is exact instead of approximate. In this functional graph (one parent each) the nodes with no
    #    CHILD can never be on a cycle, so removing them repeatedly leaves precisely the cyclic cores.
    #
    #    A pure cycle loses all of its edges deliberately: there is no principled way to pick which
    #    of three mutually-referring cards is "the" mistake, and picking one would make the published
    #    board depend on dict iteration order. Corrupt input becomes roots, not an arbitrary tree.
    child_count: dict[str, int] = {}
    for target in declared.values():
        child_count[target] = child_count.get(target, 0) + 1
    peel = [cid for cid in declared if child_count.get(cid, 0) == 0]
    while peel:
        cid = peel.pop()
        target = declared[cid]
        child_count[target] = child_count.get(target, 1) - 1
        if child_count[target] == 0 and target in declared:
            peel.append(target)
    on_cycle = {cid for cid in declared if child_count.get(cid, 0) > 0}

    # 4) With the cycles gone every remaining chain terminates, so depth is measurable. A card whose
    #    ancestor chain already reaches the bound becomes a ROOT: the top `CARD_LINEAGE_MAX_DEPTH`
    #    levels stay a tree and anything past them is published as its own family rather than
    #    silently deepening one no consumer is willing to walk.
    for cid, target in declared.items():
        if cid in on_cycle:
            continue
        walk: str | None = target
        depth = 0
        while walk is not None and walk not in on_cycle and depth < CARD_LINEAGE_MAX_DEPTH:
            walk = declared.get(walk)
            depth += 1
        if depth < CARD_LINEAGE_MAX_DEPTH:
            cards[cid].parent_card_id = target

    # 5) The inverse edge and the parent's rollup. `sorted` so two folds of the same log publish the
    #    same list; the rollup is computed over EVERY child while only the first
    #    `CARD_CHILD_LIMIT` ids are published, so a clipped parent still states its true size.
    children: dict[str, list[str]] = {}
    for cid, c in cards.items():
        if c.parent_card_id:
            children.setdefault(c.parent_card_id, []).append(cid)
    for parent_id, kids in children.items():
        kids.sort()
        parent = cards[parent_id]
        parent.child_card_ids = kids[:CARD_CHILD_LIMIT]
        parent.child_rollup = card_child_rollup([cards[k] for k in kids])
        # The concept union over EVERY child, not only the published ids — same reason the rollup
        # counts every child. Written to `child_concept_tags` and never to `concept_tags`, whose
        # `concept_source` provenance says who AUTHORED a membership and may not be handed a
        # derived union (see the field).
        union: set[str] = set()
        for k in kids:
            union.update(t for t in (cards[k].concept_tags or []) if isinstance(t, str) and t)
        # The bound is the WIRE's, shared as a constant rather than typed here: this clipped at 64
        # while `serve/public_cards.py` publishes at 32, so a direction whose children named 33+
        # distinct concepts published a truncated set AND reported its whole card projection
        # incomplete — the board-wide "card projection incomplete" banner, on a healthy board.
        parent.child_concept_tags = sorted(union)[:CARD_CONCEPT_TAG_LIMIT]


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


def derive_cards(
    st: RunState, *, card_enrichment_omissions: Mapping[tuple, int] | None = None,
) -> None:
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
    the operator overlay must run after enrichment and ranking (docs/23 decision 27),
    ``actionable`` / ``selection_ready`` read the FINAL status, and ``_apply_card_lineage`` must run
    AFTER selection readiness because ``card_kind`` is derived from the ``selection_provenance``
    that step writes. ``_apply_card_belief_lineage`` is the
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
    control_ids = _fold_merged_cards(st, identity, ledger, aliases, control_ids)
    _apply_unexecuted_discards(st, ledger)
    _apply_card_verdicts(st, ledger, control_ids)
    dropped = _apply_card_drops(st, ledger, aliases)
    building_card_nodes = _card_building_ids(st, ledger, aliases)
    _apply_card_status(st, ledger, dropped, building_card_nodes)
    _apply_card_applied_params(st, ledger)
    _apply_card_enrichment(st, ledger, aliases, card_enrichment_omissions)
    _apply_card_ranking(st, identity, ledger, aliases)
    _apply_card_operator_overlays(st, ledger, aliases)
    _apply_card_belief_lineage(st, ledger, aliases)
    _apply_card_actionable(ledger)
    _apply_card_selection_readiness(st, ledger, aliases, building_card_nodes)
    _apply_card_lineage(ledger, aliases)
    _publish_visible_cards(st, ledger, control_ids)
