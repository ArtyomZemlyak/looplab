"""Cross-run CONCEPT CAPSULES — the durable per-run concept record and the portfolio views over it.

Split out of `memory.py` (doc 25 EM-10), which was named for the episodic case library its docstring
describes and had grown to hold five unrelated subsystems. This is the capsule one: what a valid
capsule record IS (`_valid_capsule_record`, `_dedup_valid_capsules`), the receipts that say how
complete it is (`_capsule_completeness`, `_capsule_source_summary`), the store that persists it
(`ConceptCapsuleStore`), and the portfolio overview projection built from it.

Moved VERBATIM, together with the capsule/overview bound constants that only these functions use.

`portfolio_concept_graph` used to live here too and is GONE — not deprecated, removed. It called
itself "the GLOBAL cross-run concept MAP", and so does the run list's `Concepts` view, and the two
read different corpora: capsules exist only for runs that finished and published one (measured on
this box: 3, against 15 tagged runs in the list). Two functions claiming to be one lab's concept map
is a population disagreement waiting to be rendered. The map is now ONE fold,
`search/concept_lens.py::concept_map`, which takes per-run concept SETS and lets the caller own the
population — capsules for an agent tool, the scoped run rows for the browser.

The two helpers this band ALSO needed — `fingerprint_similarity` and the None-tolerant metric
predicate — moved DOWN to `core` first (`core/text`, `core/fitness.finite_or_absent_metric`). That
order is the whole reason this split is a layering rather than a cycle: `memory` re-exports this
module, so this module must not import `memory`, and both now reach the shared helpers downward.

`memory.py` re-exports every name here, so both spellings resolve to the SAME objects and existing
imports and monkeypatch seams (`tools/` reaches for `_dedup_valid_capsules`,
`_portfolio_concept_overview_data`, `_capsule_rows` &c.) are unaffected.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from looplab.core.fitness import finite_or_absent_metric as _is_finite_metric
from looplab.core.text import fingerprint_similarity
from looplab.core.receipts import ReceiptRows, bounded_receipt_count
from looplab.core.models import NODE_CONCEPT_PROVENANCE_CLASSIFIER
from looplab.core.jsonlio import (read_jsonl_lenient_with_health,
                                  replace_jsonl_rows_atomic_preserving_quarantine)

CONCEPT_CAPSULE_VERSION = 2

_LEGACY_CONCEPT_CAPSULE_VERSION = 1

_MAX_CAPSULE_ID_CHARS = 500

_MAX_CAPSULE_TOKEN_CHARS = 500

_MAX_CAPSULE_FINGERPRINT = 256

_MAX_CAPSULE_CONCEPTS = 256

_MAX_CAPSULE_OUTCOMES = 256

_MAX_CAPSULE_SOURCE_ITEMS = (1 << 31) - 1

_MAX_OVERVIEW_CONCEPTS = 512

_MAX_OVERVIEW_RUNS_PER_CONCEPT = 64

_MAX_OVERVIEW_RUN_CARDS = 512

_MAX_OVERVIEW_CARD_CONCEPTS = 64

_EMPTY_CAPSULE_STORE_HEALTH = {
    "source_store_complete": True,
    "source_rows_total": 0,
    "source_rows_quarantined": 0,
    "source_malformed_rows": 0,
    "source_invalid_capsule_rows": 0,
    "source_duplicate_run_rows": 0,
}

class _CapsuleRows(ReceiptRows):
    """List-compatible capsule snapshot carrying file/schema quarantine health through projections."""

    CARRIED_FIELDS = ("source_health",)
    __slots__ = CARRIED_FIELDS

    def __init__(self, rows=(), *, source_health: Optional[dict] = None):
        super().__init__(rows)
        self.source_health = {**_EMPTY_CAPSULE_STORE_HEALTH, **(source_health or {})}

def _capsule_rows(rows=(), *, source=None) -> _CapsuleRows:
    """Copy rows while preserving the originating store health receipt."""
    origin = source if source is not None else rows
    health = getattr(origin, "source_health", None)
    if health is None and isinstance(origin, (list, tuple)):
        health = {**_EMPTY_CAPSULE_STORE_HEALTH, "source_rows_total": len(origin)}
    return _CapsuleRows(rows, source_health=health)

def _filter_capsule_rows(rows, predicate) -> _CapsuleRows:
    """Filter a capsule snapshot without laundering quarantined source rows into exact absence."""
    source = rows if isinstance(rows, (list, tuple)) else []
    # `_capsule_rows` first, because inheriting the receipt from a plain list is capsule-specific
    # (it derives `source_rows_total` from the origin); the narrowing itself is the shared
    # receipt-preserving projection rather than a comprehension that would drop it (EM-09).
    return _capsule_rows(source, source=source).filter(predicate)

def _capsule_concept_evidence_completeness(
        capsule: dict,
) -> Optional[tuple[Optional[int], Optional[int], bool, Optional[bool]]]:
    """Read the classifier-membership producer receipt.

    The receipt is additive over capsule v2.  A v2 row written before this receipt remains a valid
    positive observation, but its membership denominator is unknowable and therefore incomplete.
    """
    keys = (
        "concept_evidence_nodes_total",
        "concept_evidence_nodes_incomplete",
        "concept_evidence_complete",
    )
    present = [key in capsule for key in keys]
    if not any(present):
        return None, None, False, None
    if not all(present):
        return None
    total, incomplete, complete = (capsule[key] for key in keys)
    # `incomplete` is bounded by `total`, not by the collection cap — a subset denominator, so its
    # own ceiling is the total it is a subset of.
    if (not bounded_receipt_count(total, _MAX_CAPSULE_SOURCE_ITEMS)
            or not bounded_receipt_count(incomplete, total)
            or type(complete) is not bool):
        return None
    observed = capsule.get("concept_evidence_observed")
    if "concept_evidence_observed" not in capsule:
        # Old v2 positive/partial rows remain useful.  An old EMPTY row, however, did not distinguish
        # "classifier observed zero memberships" from "classifier never ran", so its absence is unknown.
        observed = True if total > 0 or capsule.get("concepts") or capsule.get("concept_outcomes") else None
    elif type(observed) is not bool:
        return None
    if observed is False:
        if total != 0 or incomplete != 0 or capsule.get("concepts") or capsule.get("concept_outcomes"):
            return None
        if complete is not False:
            return None
        return total, incomplete, False, False
    if complete != (incomplete == 0):
        return None
    # A pre-marker empty v2 row is readable but cannot prove authoritative absence.
    return total, incomplete, complete if observed is True else False, observed

def _capsule_completeness(
        capsule: dict, stem: str, included: int,
) -> Optional[tuple[Optional[int], Optional[int], bool]]:
    """Read one additive capsule completeness triplet; old v2 rows are valid but UNKNOWN/partial."""
    total_key, omitted_key, complete_key = f"{stem}_total", f"{stem}_omitted", f"{stem}_complete"
    present = [key in capsule for key in (total_key, omitted_key, complete_key)]
    if not any(present):
        # Old v2 writers silently capped collections. Their retained observations remain useful, but neither
        # the original total nor completeness can be reconstructed honestly from the durable row.
        return None, None, False
    if not all(present):
        return None
    total, omitted, complete = capsule[total_key], capsule[omitted_key], capsule[complete_key]
    if (not bounded_receipt_count(total, _MAX_CAPSULE_SOURCE_ITEMS)
            or not bounded_receipt_count(omitted, _MAX_CAPSULE_SOURCE_ITEMS)
            or type(complete) is not bool
            or total < included or omitted != total - included):
        return None
    if stem in ("concepts", "concept_outcomes"):
        evidence_meta = _capsule_concept_evidence_completeness(capsule)
        if evidence_meta is None:
            return None
        if evidence_meta[0] is None:
            # Pre-receipt v2 writers used collection truncation as their only completeness boundary.
            # Keep those rows readable, but never repeat their optimistic flag downstream.
            if complete != (omitted == 0):
                return None
            complete = False
        elif evidence_meta[3] is not True:
            # an observed-empty marker is what separates a deletion tombstone from an
            # unknown/never-classified empty projection.  Legacy or explicit unknown empty rows remain
            # readable, but their optimistic collection flags are never repeated downstream.
            if complete != (omitted == 0 if evidence_meta[3] is None else False):
                return None
            complete = False
        elif complete != (omitted == 0 and evidence_meta[2]):
            return None
    elif complete != (omitted == 0):
        return None
    return total, omitted, complete

def _capsule_source_summary(capsules: list[dict]) -> dict:
    """Aggregate capsule omissions plus the durable store's quarantine/read-health receipt."""
    capsules = _dedup_valid_capsules(capsules)
    concept_omitted = outcome_omitted = partial = unknown = 0
    for capsule in capsules:
        evidence_meta = _capsule_concept_evidence_completeness(capsule)
        concept_meta = _capsule_completeness(capsule, "concepts", len(capsule.get("concepts") or []))
        outcome_meta = _capsule_completeness(
            capsule, "concept_outcomes", len(capsule.get("concept_outcomes") or {}))
        # Callers pass validated rows; keep this total if a future caller violates that private contract.
        if evidence_meta is None or concept_meta is None or outcome_meta is None:
            partial += 1
            unknown += 1
            continue
        concept_omitted += concept_meta[1] or 0
        outcome_omitted += outcome_meta[1] or 0
        unknown += int(
            evidence_meta[3] is not True
            or concept_meta[0] is None or outcome_meta[0] is None)
        partial += int(
            not evidence_meta[2] or not concept_meta[2] or not outcome_meta[2])
    store_health = {
        **_EMPTY_CAPSULE_STORE_HEALTH,
        **(getattr(capsules, "source_health", None) or {}),
    }
    return {
        # quarantine keeps poisoned content out, but it cannot turn an unreadable durable row
        # into proof of absence. Completeness crosses both the per-capsule bounds and file/schema health.
        "source_complete": partial == 0 and store_health["source_store_complete"] is True,
        "partial_capsules": partial,
        "source_unknown_capsules": unknown,
        "source_concepts_omitted": concept_omitted,
        "source_outcomes_omitted": outcome_omitted,
        **store_health,
    }

def _capsule_fingerprint_scope_complete(capsule: dict) -> bool:
    """Whether a capsule's persisted fingerprint is an exact source projection.

    Related-task transfer treats this as an applicability boundary.  Exact ``task_id`` matches do not need
    the fuzzy fingerprint, but a capped or legacy-unknown fingerprint must never authorize a foreign task.
    """
    if not isinstance(capsule, dict):
        return False
    fingerprint = capsule.get("fingerprint")
    if not isinstance(fingerprint, list):
        return False
    meta = _capsule_completeness(capsule, "fingerprint", len(fingerprint))
    return meta is not None and meta[2] is True

def _valid_capsule_record(capsule) -> bool:
    """Validate one durable capsule without coercing semantic identity.

    Oversized or ill-typed rows are quarantined rather than truncated: truncating a run id or concept
    slug could alias two distinct durable entities.
    """
    if not isinstance(capsule, dict):
        return False
    # missing `v` is legacy, never the current schema. Defaulting it to the current version
    # would silently bless old proposer-authored labels after a schema bump.
    version = capsule.get("v", _LEGACY_CONCEPT_CAPSULE_VERSION)
    run_id = capsule.get("run_id")
    task_id = capsule.get("task_id", "")
    fingerprint = capsule.get("fingerprint")
    concepts = capsule.get("concepts")
    outcomes = capsule.get("concept_outcomes", {})
    # Phase 1 profit signs are ADDITIVE over v2: a missing field defaults to {} (old capsules stay valid);
    # a present one must be a bounded dict of {concept -> -1|0|1} (bool excluded — it is an int subclass).
    signs = capsule.get("concept_signs", {})
    if (version != CONCEPT_CAPSULE_VERSION
            or capsule.get("concept_evidence") != NODE_CONCEPT_PROVENANCE_CLASSIFIER
            or not isinstance(run_id, str) or not run_id or len(run_id) > _MAX_CAPSULE_ID_CHARS
            or not isinstance(task_id, str) or len(task_id) > _MAX_CAPSULE_ID_CHARS
            # every v2 producer wrote an explicit direction.  Treat a missing field as
            # quarantine evidence instead of silently inventing ``min`` and potentially reversing the
            # meaning of retained outcomes/rank signs in unbound portfolio projections.
            or capsule.get("direction") not in ("min", "max")
            or not _is_finite_metric(capsule.get("best_metric"))
            or not isinstance(fingerprint, list) or len(fingerprint) > _MAX_CAPSULE_FINGERPRINT
            or not isinstance(concepts, list) or len(concepts) > _MAX_CAPSULE_CONCEPTS
            or not isinstance(outcomes, dict) or len(outcomes) > _MAX_CAPSULE_OUTCOMES
            or not isinstance(signs, dict) or len(signs) > _MAX_CAPSULE_OUTCOMES):
        return False
    if any(not isinstance(value, str) or not value or len(value) > _MAX_CAPSULE_TOKEN_CHARS
           for value in fingerprint + concepts):
        return False
    from looplab.core.concepts import valid_concept_id
    concept_set = set(concepts)
    # a capsule is a durable evidence boundary. Quarantine the entire poisoned row instead of
    # letting one invalid/out-of-membership key disagree with canonical run cards and concept projections.
    if (len(concept_set) != len(concepts)
            or any(not valid_concept_id(value) for value in concepts)
            or any(not valid_concept_id(key) or key not in concept_set for key in outcomes)
            or any(not valid_concept_id(key) or key not in outcomes for key in signs)):
        return False
    if any(not isinstance(key, str) or not key or len(key) > _MAX_CAPSULE_TOKEN_CHARS
           or type(value) is not int or value not in (-1, 0, 1) for key, value in signs.items()):
        return False   # `type(value) is not int` rejects bool (int subclass) AND float 1.0 in one test
    if not all(isinstance(key, str) and key and len(key) <= _MAX_CAPSULE_TOKEN_CHARS
               and _is_finite_metric(value) for key, value in outcomes.items()):
        return False
    evidence_meta = _capsule_concept_evidence_completeness(capsule)
    if (evidence_meta is not None and evidence_meta[0] is not None
            and (concepts or outcomes) and evidence_meta[0] == 0):
        return False
    return (evidence_meta is not None
            and _capsule_completeness(capsule, "fingerprint", len(fingerprint)) is not None
            and _capsule_completeness(capsule, "concepts", len(concepts)) is not None
            and _capsule_completeness(capsule, "concept_outcomes", len(outcomes)) is not None)

def _dedup_valid_capsules(capsules) -> _CapsuleRows:
    """Quarantine + deterministically de-duplicate a raw capsule sequence: keep only valid records, collapse
    duplicate run ids to ONE, and return them in sorted-run-id order. The shared portfolio read-models feed
    this RAW decoded rows (a caller may concatenate shards or hand a pre-compaction file), so the collision
    winner must be INPUT-ORDER-INDEPENDENT — pick the row with the lexicographically-greatest canonical JSON
    (a stable representative), not "last seen in list order". The store path has unique run ids, so it never
    collides; this only bites the raw-row callers the docstring promises to tolerate."""
    source = capsules if isinstance(capsules, (list, tuple)) else []
    inherited = {
        **_EMPTY_CAPSULE_STORE_HEALTH,
        **(getattr(source, "source_health", None) or {}),
    }
    by_run: dict[str, dict] = {}
    invalid_capsules = valid_rows = 0
    for capsule in source:
        if not _valid_capsule_record(capsule):
            invalid_capsules += 1
            continue
        valid_rows += 1
        rid = capsule["run_id"]
        prev = by_run.get(rid)
        if prev is None or json.dumps(capsule, sort_keys=True) > json.dumps(prev, sort_keys=True):
            by_run[rid] = capsule
    duplicates = valid_rows - len(by_run)
    malformed = int(inherited.get("source_malformed_rows", 0) or 0)
    invalid = max(int(inherited.get("source_invalid_capsule_rows", 0) or 0), invalid_capsules)
    duplicate_rows = max(int(inherited.get("source_duplicate_run_rows", 0) or 0), duplicates)
    quarantined = max(
        int(inherited.get("source_rows_quarantined", 0) or 0),
        malformed + invalid + duplicate_rows,
    )
    source_total = int(inherited.get("source_rows_total", 0) or 0)
    if not hasattr(source, "source_health"):
        source_total = len(source)
    health = {
        "source_store_complete": inherited.get("source_store_complete") is True and quarantined == 0,
        "source_rows_total": source_total,
        "source_rows_quarantined": quarantined,
        "source_malformed_rows": malformed,
        "source_invalid_capsule_rows": invalid,
        "source_duplicate_run_rows": duplicate_rows,
    }
    return _CapsuleRows(
        (by_run[run_id] for run_id in sorted(by_run)), source_health=health)

_CONCEPT_NEUTRAL_BAND_FRAC = 0.10

def _concept_profit_signs(outcomes: dict, direction: str) -> dict:
    """PART V Phase 1: a direction-normalized, scale-free RANK-WITHIN-RUN profit sign per concept.

    Raw metrics do NOT compare across runs/tasks (the portfolio overview refuses to aggregate them), so the
    sign is deliberately RELATIVE, not a fixed-baseline profit: "did this concept's best outcome land in the
    BETTER or WORSE part of THIS run's own field of concepts, judged in THIS run's direction". That per-run
    rank IS direction-/scale-normalized, so it aggregates across runs into an advisory tendency (a concept
    that consistently ranks well across many DIFFERENT sibling sets is a decent bet) — but it is a rank, not
    causal proof, and the baseline-stable "did ADDING this concept beat the parent" signal is Phase 3's
    per-node delta. Baseline is the run's own MEDIAN outcome; a NEUTRAL BAND (a fraction of the run's outcome
    spread) around it keeps near-median concepts off both sides, so the split is not forced ~50/50. +1 = clearly
    better half, -1 = clearly worse half, 0 = neutral. Fewer than two outcomes → no signal (empty). Pure/
    deterministic; keys mirror `outcomes` (already bounded by the caller)."""
    values = sorted(v for v in outcomes.values() if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v))
    if len(values) < 2:
        return {}
    n = len(values)
    baseline = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0
    band = _CONCEPT_NEUTRAL_BAND_FRAC * (values[-1] - values[0])   # within-run, scale-relative neutral zone
    signs: dict[str, int] = {}
    for concept, metric in outcomes.items():
        if not (isinstance(metric, (int, float)) and not isinstance(metric, bool) and math.isfinite(metric)):
            continue
        if abs(metric - baseline) <= band:
            signs[concept] = 0
        elif (metric < baseline) if direction == "min" else (metric > baseline):
            signs[concept] = 1
        else:
            signs[concept] = -1
    return signs

def concept_profit_tendencies(concept_rows, *, limit: Optional[int] = None) -> dict:
    """Split rolled-up concept rows (each with n_helped/n_neutral/n_hurt, from `portfolio_concept_overview`)
    into CONSISTENT, multi-run help/hurt tendencies — the SINGLE source of truth for every advisory surface
    (the context pack, the cross_run_atlas tool, any future one), so the threshold can never silently diverge
    between them. A concept qualifies when it carried a sign in ≥2 runs, landed on ONE side in ≥2 of them, and
    net that way (n_helped>n_hurt for help, mirror for hurt) — so a concept can never be in both, and a
    mixed/thin one is in neither. Returns {"helps": [(concept, n_helped), …], "hurts": [(concept, n_hurt), …]},
    each ranked by that count desc then name. Pure/deterministic; ADVISORY tendency, never a selection input."""
    rows = concept_rows if isinstance(concept_rows, (list, tuple)) else []

    def _int(x) -> int:                          # torn/hand-built rows may carry null/str counts
        return x if isinstance(x, int) and not isinstance(x, bool) else 0

    def _pick(is_help: bool) -> list:
        out = []
        for e in rows:
            if not isinstance(e, dict):
                continue
            h, n, t = _int(e.get("n_helped")), _int(e.get("n_neutral")), _int(e.get("n_hurt"))
            if h + n + t < 2:
                continue
            if (h >= 2 and h > t) if is_help else (t >= 2 and t > h):
                out.append((str(e.get("concept") or ""), h if is_help else t))
        out.sort(key=lambda kv: (-kv[1], kv[0]))
        return out[:limit] if limit else out

    return {"helps": _pick(True), "hurts": _pick(False)}

def build_concept_capsule(*, run_id: str, fingerprint: list[str], direction: str,
                          concepts, best_metric=None, concept_outcomes: Optional[dict] = None,
                          task_id: str = "", concept_evidence_nodes_total: Optional[int] = None,
                          concept_evidence_nodes_incomplete: int = 0,
                          concept_evidence_observed: Optional[bool] = None) -> dict:
    """A compact per-run CONCEPT capsule — the cross-run bridge (§21.20 Step 2). It records WHICH
    concepts a run explored (the shipped per-run `node_concepts` tags — no new tagger) and how it went,
    keyed by `task_fingerprint`, so a later SIMILAR run can answer "was this tried across runs, and
    with what result?" and feed `grade_novelty`'s `prior_concepts` (D3 level 3 = surface prior, never
    reject). Carries a schema `v` + `task_id` scope; deliberately small and JSON-flat (memory-store data,
    not a fold event)."""
    fingerprint_collection = isinstance(fingerprint, (list, tuple, set))
    fingerprint_source = fingerprint if fingerprint_collection else []
    concepts_collection = isinstance(concepts, (list, tuple, set))
    concepts_source = concepts if concepts_collection else []
    valid_fingerprint = sorted({token for token in fingerprint_source
                                if isinstance(token, str) and token
                                and len(token) <= _MAX_CAPSULE_TOKEN_CHARS})
    invalid_fingerprint = sum(
        not isinstance(token, str) or not token or len(token) > _MAX_CAPSULE_TOKEN_CHARS
        for token in fingerprint_source
    ) + int(not fingerprint_collection)
    bounded_fingerprint = valid_fingerprint[:_MAX_CAPSULE_FINGERPRINT]
    if not isinstance(direction, str) or direction not in ("min", "max"):
        # direction controls both sign polarity and task-family scope. A writer typo must fail
        # closed, never be coerced to `min` and persisted as inverted cross-run evidence.
        raise ValueError("concept capsule direction must be exactly 'min' or 'max'")
    normalized_direction = direction
    from looplab.core.concepts import valid_concept_id
    valid_concepts = sorted({raw for raw in concepts_source if valid_concept_id(raw)})
    invalid_concepts = sum(not valid_concept_id(raw) for raw in concepts_source) + int(not concepts_collection)
    bounded_concepts = valid_concepts[:_MAX_CAPSULE_CONCEPTS]
    concept_set = set(valid_concepts)
    outcomes_mapping = isinstance(concept_outcomes, dict)
    raw_outcomes = concept_outcomes if outcomes_mapping else {}
    all_outcomes: dict[str, object] = {}
    for raw_key, value in sorted(raw_outcomes.items(), key=lambda item: str(item[0])):
        if not valid_concept_id(raw_key) or raw_key not in concept_set or not _is_finite_metric(value):
            continue
        all_outcomes[raw_key] = value
    # compute the run-relative baseline over the COMPLETE valid source field. Truncating first
    # shifts its median/neutral band and can reverse the persisted sign of retained concepts.
    all_signs = _concept_profit_signs(all_outcomes, normalized_direction)
    bounded_concept_set = set(bounded_concepts)
    bounded_outcomes = dict(list((
        (key, value) for key, value in sorted(all_outcomes.items()) if key in bounded_concept_set
    ))[:_MAX_CAPSULE_OUTCOMES])
    concept_signs = {key: all_signs[key] for key in bounded_outcomes if key in all_signs}
    # Invalid source entries are omitted evidence too. Count them in the receipt so filtering cannot turn a
    # poisoned input into a capsule that claims its source was exact/complete.
    concepts_total = len(valid_concepts) + invalid_concepts
    concepts_omitted = concepts_total - len(bounded_concepts)
    outcomes_total = len(raw_outcomes) + int(concept_outcomes is not None and not outcomes_mapping)
    outcomes_omitted = outcomes_total - len(bounded_outcomes)
    fingerprint_total = len(valid_fingerprint) + invalid_fingerprint
    fingerprint_omitted = fingerprint_total - len(bounded_fingerprint)
    if concept_evidence_nodes_total is None:
        # Pure callers provide a complete concept collection rather than a folded RunState. Model that
        # collection as one evidence unit when non-empty; the engine writer supplies the exact active-node
        # denominator explicitly.
        concept_evidence_nodes_total = int(bool(concepts_source))
    if (type(concept_evidence_nodes_total) is not int
            or type(concept_evidence_nodes_incomplete) is not int
            or not 0 <= concept_evidence_nodes_total <= _MAX_CAPSULE_SOURCE_ITEMS
            or not 0 <= concept_evidence_nodes_incomplete <= concept_evidence_nodes_total):
        # these counts are the durable denominator for classifier memberships.  Coercion or
        # clipping here would turn a corrupt producer receipt into permission to infer absent concepts.
        raise ValueError("concept capsule evidence-node receipt is invalid")
    if concept_evidence_observed is None:
        concept_evidence_observed = bool(concepts_source) or concept_evidence_nodes_total > 0
    if type(concept_evidence_observed) is not bool:
        raise ValueError("concept capsule observed-evidence receipt is invalid")
    if (not concept_evidence_observed
            and (concept_evidence_nodes_total or concepts_source or raw_outcomes)):
        raise ValueError("unobserved concept evidence cannot carry memberships or outcomes")
    concept_evidence_complete = (
        concept_evidence_observed and concept_evidence_nodes_incomplete == 0)
    return {
        "v": CONCEPT_CAPSULE_VERSION,
        "concept_evidence": NODE_CONCEPT_PROVENANCE_CLASSIFIER,
        # ``observed=true`` plus empty collections is a same-run tombstone. ``false`` is an
        # unknown snapshot and must stay partial; this additive bit keeps old positive v2 rows readable.
        "concept_evidence_observed": concept_evidence_observed,
        "concept_evidence_nodes_total": concept_evidence_nodes_total,
        "concept_evidence_nodes_incomplete": concept_evidence_nodes_incomplete,
        "concept_evidence_complete": concept_evidence_complete,
        "run_id": str(run_id or ""),
        "task_id": str(task_id or ""),
        "fingerprint": bounded_fingerprint,
        "fingerprint_total": fingerprint_total,
        "fingerprint_omitted": fingerprint_omitted,
        "fingerprint_complete": fingerprint_omitted == 0,
        "direction": normalized_direction,
        "concepts": bounded_concepts,
        "concepts_total": concepts_total,
        "concepts_omitted": concepts_omitted,
        "concepts_complete": concepts_omitted == 0 and concept_evidence_complete,
        "best_metric": best_metric if _is_finite_metric(best_metric) else None,
        "concept_outcomes": bounded_outcomes,
        "concept_outcomes_total": outcomes_total,
        "concept_outcomes_omitted": outcomes_omitted,
        "concept_outcomes_complete": outcomes_omitted == 0 and concept_evidence_complete,
        # PART V Phase 1: a direction-normalized RANK-WITHIN-RUN sign per concept (+1 clearly-better-half /
        # 0 neutral / -1 clearly-worse-half vs this run's own field) — additive over v2 (old capsules lack
        # it, readers default {}). Relative rank, not causal profit; the per-node delta is Phase 3.
        "concept_signs": concept_signs,
    }

class ConceptCapsuleStore:
    """Cross-run concept memory with one current v2 row per local ``run_id``.

    Upsert uses a required interprocess lock and quarantine-preserving atomic replacement. Exact task id
    takes precedence on read; related-task transfer is allowed only when a complete fingerprint receipt
    passes the bounded similarity rule. ``run_id`` is not yet a portfolio-wide incarnation id, so two
    independent run roots sharing one global memory directory can collide on the same display id.
    The store performs no tagging and holds no engine state.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capsules: list[dict] = []
        self.source_health = dict(_EMPTY_CAPSULE_STORE_HEALTH)
        self._reload()

    @staticmethod
    def _valid_capsule(c: dict) -> bool:
        """Per-row schema guard: `dicts_only` alone lets a row with an int `fingerprint` or a
        string `concepts` poison retrieval (a string iterates into CHARACTER concepts). Quarantine the
        bad row instead of letting it disable the feature: require the list-typed fields to be lists and
        `run_id` to be a non-empty string. Unknown extra fields are fine (forward-compat)."""
        # Missing `v` and v1 predate concept-producer provenance. They cannot be distinguished from
        # proposer-authored self-labels, so both fail closed alongside explicit unknown versions.
        return _valid_capsule_record(c)

    def _reload(self) -> None:
        rows, read_health = read_jsonl_lenient_with_health(
            self.path, loads=json.loads, dicts_only=True)
        self.capsules = [c for c in rows if self._valid_capsule(c)]   # drop poisoned rows, keep the rest
        invalid_capsules = int(read_health["invalid_shape_lines"]) + len(rows) - len(self.capsules)
        duplicate_runs = len(self.capsules) - len({c["run_id"] for c in self.capsules})
        malformed = int(read_health["malformed_lines"])
        quarantined = malformed + invalid_capsules + duplicate_runs
        self.source_health = {
            "source_store_complete": quarantined == 0,
            "source_rows_total": int(read_health["source_lines"]),
            "source_rows_quarantined": quarantined,
            "source_malformed_rows": malformed,
            "source_invalid_capsule_rows": invalid_capsules,
            "source_duplicate_run_rows": duplicate_runs,
        }

    def add(self, capsule: dict) -> bool:
        """Upsert by `run_id` under the same interprocess lock the case/lesson stores use, re-reading
        inside the lock so a concurrent run's capsule survives. Returns True once stored."""
        from looplab.events.eventstore import _interprocess_lock
        if not self._valid_capsule(capsule):
            return False
        # run_id is only a run-root-local label (separate checkouts default to run_local),
        # while memory_dir is global by default. Unrelated runs therefore replace each other's capsule,
        # and current-run exclusion hides the older row first. Key upsert/exclusion by a persisted
        # globally unique run-incarnation UID; retain run_id only for display.
        rid = str(capsule.get("run_id") or "")
        with _interprocess_lock(Path(str(self.path) + ".lock"), required=True):
            # quarantine is a read policy, not permission to erase old/future durable data.
            # Preserve raw malformed AND decoded future rows; supersede only the exact run id.
            replace_jsonl_rows_atomic_preserving_quarantine(
                self.path, [capsule],
                # matching an opaque key is not permission to delete a future/invalid schema.
                # Supersede only a row this reader fully understands; keep an unknown same-run record for
                # explicit repair/migration alongside the new current-schema capsule.
                replace_if=lambda row: (self._valid_capsule(row)
                                        and str(row.get("run_id") or "") == rid),
                loads=json.loads, dumps=json.dumps,
            )
            self._reload()
        return True

    def prior_capsules(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "", task_id: str = "") -> list[tuple[float, dict]]:
        """Prior-run capsules in an exact task or with an exact fingerprint clearing ``min_sim``.

        Fingerprint matching is Jaccard/universal-aware because the fingerprint itself already is.  Exact
        task identity, when supplied, scores 1.0 without consulting a potentially legacy/capped fingerprint.
        Results are most-similar first, excluding this run; each tuple is ``(similarity, capsule)`` so a
        surfacing cue can rank and cite by run.
        """
        # NOTE (full-CR TODO §21.20.13 CR2a): O(portfolio) scan+sort per call, on top of the
        # whole-file reload/rewrite this store inherits from JsonlCaseLibrary — fine at tens–hundreds of
        # runs, replaced by a bounded scope/version-keyed index snapshot at portfolio scale.
        out = []
        for c in self.capsules:
            if exclude_run_id and str(c.get("run_id") or "") == str(exclude_run_id):
                continue
            exact_task = bool(task_id) and str(c.get("task_id") or "") == str(task_id)
            # the writer bounds fingerprints.  A retained prefix (or a pre-receipt v2 row)
            # can inflate Jaccard and is not authority for related-task transfer.  Exact task identity is
            # still usable because it does not depend on the lossy fingerprint projection.
            if not exact_task and not _capsule_fingerprint_scope_complete(c):
                continue
            sim = 1.0 if exact_task else fingerprint_similarity(fingerprint, c.get("fingerprint") or [])
            if sim >= min_sim:
                out.append((sim, c))
        out.sort(key=lambda t: (-t[0], str(t[1].get("run_id") or "")))
        return out

    def prior_concepts(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "", task_id: str = "") -> set[str]:
        """The UNION of concepts explored by similar prior runs — exactly the `set[str]` shape
        `grade_novelty(prior_concepts=…)` consumes to fire D3 level 3 ("tried across runs")."""
        acc: set[str] = set()
        for _sim, c in self.prior_capsules(
                fingerprint, min_sim=min_sim, exclude_run_id=exclude_run_id, task_id=task_id):
            acc.update(str(x) for x in (c.get("concepts") or []))
        return acc

    def all(self) -> list[dict]:
        return _CapsuleRows(self.capsules, source_health=self.source_health)

def portfolio_concept_overview(capsules: list[dict], *, aliases: Optional[dict] = None,
                               splits: Optional[dict] = None) -> dict:
    """A cross-run portfolio read-model over concept capsules (§21.20 Step 3 — 'what has been tried across
    the portfolio'). Pure/deterministic, no LLM/IO, drillable to `run_id`. For each concept it lists the
    runs that explored it with THEIR OWN outcome (run_id, metric, direction) — deliberately NOT a single
    cross-run 'best', because raw metrics from different tasks/directions are not comparable without a
    shared contract (§21.20.1). Also emits a per-run card (concept count + the run's own best_metric).
    `aliases` (from `load_concept_aliases`, CR1a) canonicalizes concept slugs at read time: merged aliases
    collapse to one concept and purged concepts drop; `splits` (from `load_concept_splits`) re-tags a coarse
    concept per that run's OWN sibling concepts. The raw per-run tags are untouched (non-destructive)."""
    return _portfolio_concept_overview_data(
        capsules, aliases=aliases, splits=splits)[0]

def _portfolio_concept_overview_data(capsules: list[dict], *, aliases: Optional[dict] = None,
                                     splits: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """Build the public bounded overview and its full internal concept rows from one exact snapshot.

    The second value is for bounded derived read-models that must aggregate before their own display cap;
    it is deliberately private so callers cannot accidentally expose an unbounded portfolio response.
    """
    from looplab.engine.concept_registry import canonicalize_concept, canonicalize_concepts

    valid_capsules = _dedup_valid_capsules(capsules)

    per_concept: dict[str, dict] = {}
    for c in valid_capsules:
        rid = str(c.get("run_id") or "")
        oc = c.get("concept_outcomes") or {}
        outcome_meta = _capsule_completeness(
            c, "concept_outcomes", len(c.get("concept_outcomes") or {}))
        # pre-receipt v2 writers could truncate BEFORE computing rank signs. Keep their positive
        # concept/outcome observations, but never aggregate a sign whose comparison field may be incomplete.
        signs = (c.get("concept_signs") or {}) if outcome_meta and outcome_meta[2] else {}
        direction = str(c.get("direction") or "min")
        raw = list(c.get("concepts") or [])
        # Deterministic per-(canonical, run) aggregation: canonicalize each raw slug through the shared
        # alias-source -> split -> alias-target pipeline,
        # then collapse the run's raw concepts that map to the SAME canonical into ONE run-row, so a run
        # never appears twice for one concept. The row's metric is the outcome of the sorted-first
        # raw concept that HAS an outcome (deterministic tie-break), else None.
        by_canon: dict[str, list] = {}
        for i, concept in enumerate(raw):
            key = canonicalize_concept(concept, sibling_concepts=raw[:i] + raw[i + 1:],
                                       aliases=aliases, splits=splits)
            if not key:
                continue                          # purged concept -> dropped from cross-run views
            by_canon.setdefault(key, []).append(concept)
        for key, raws in by_canon.items():
            observed = [oc[r] for r in sorted(raws) if r in oc and oc[r] is not None]
            # governance asserts collapsed raw labels are one canonical technique. Its run
            # outcome is therefore the best retained observation in THIS run's direction, not the value of
            # whichever alias sorts first (which can present a losing sibling beside a winning canonical).
            metric = ((min(observed) if direction == "min" else max(observed)) if observed else None)
            # When several raw slugs collapse to ONE canonical (operator alias/split), COMBINE their signs
            # by NET rather than taking the sorted-first — else a merge silently drops the loser when two
            # raws landed on opposite sides of the run's median. sign(sum): majority side, tie -> neutral.
            run_signs = [signs.get(r) for r in sorted(raws) if signs.get(r) is not None]
            total = sum(run_signs)
            sign = None if not run_signs else (1 if total > 0 else -1 if total < 0 else 0)
            e = per_concept.setdefault(key, {"concept": key, "_runs": {}})
            e["_runs"][rid] = {
                "run_id": rid,
                "task_id": str(c.get("task_id") or ""),
                "metric": metric,
                "direction": direction,
                "sign": sign,
            }
    concepts = []
    for e in per_concept.values():
        all_runs = [e["_runs"][run_id] for run_id in sorted(e["_runs"])]
        # Phase 1 profit rollup: signs are direction-normalized, so counting them ACROSS runs is legitimate
        # even though raw metrics above are deliberately NOT aggregated (§21.20.1). Runs with no signal omit.
        row = {"concept": e["concept"], "n_runs": len(all_runs),
               "n_helped": sum(1 for r in all_runs if r.get("sign") == 1),
               "n_neutral": sum(1 for r in all_runs if r.get("sign") == 0),
               "n_hurt": sum(1 for r in all_runs if r.get("sign") == -1),
               "runs": all_runs[:_MAX_OVERVIEW_RUNS_PER_CONCEPT]}
        if len(all_runs) > len(row["runs"]):
            row["runs_omitted"] = len(all_runs) - len(row["runs"])
        concepts.append(row)
    concepts.sort(key=lambda e: (-e["n_runs"], e["concept"]))   # most-explored first, then name
    cards = []
    for c in valid_capsules:
        canonical = canonicalize_concepts(c.get("concepts") or [], aliases=aliases, splits=splits)
        evidence_meta = _capsule_concept_evidence_completeness(c)
        concept_meta = _capsule_completeness(c, "concepts", len(c.get("concepts") or []))
        outcome_meta = _capsule_completeness(
            c, "concept_outcomes", len(c.get("concept_outcomes") or {}))
        assert evidence_meta is not None and concept_meta is not None and outcome_meta is not None
        # The overview must apply normalization even with empty governance maps; otherwise
        # `Hard-Neg` and `hard-neg` are one UID in the registry but two portfolio concepts/cards.
        card = {"run_id": str(c.get("run_id") or ""), "n_concepts": len(canonical),
                 "best_metric": c.get("best_metric"), "direction": str(c.get("direction") or "min"),
                 "concepts": canonical[:_MAX_OVERVIEW_CARD_CONCEPTS],
                 # retained labels are positive observations, not a complete assignment
                 # denominator.  Preserve the producer receipt on every run card so a mixed/partial run
                 # cannot be mistaken for an exact concept inventory after aggregation.
                 "source_concept_evidence_nodes_total": evidence_meta[0],
                 "source_concept_evidence_nodes_incomplete": evidence_meta[1],
                 "source_concept_evidence_complete": evidence_meta[2],
                 "source_concepts_total": concept_meta[0],
                 "source_concepts_omitted": concept_meta[1],
                 "source_concepts_complete": concept_meta[2],
                 "source_outcomes_total": outcome_meta[0],
                 "source_outcomes_omitted": outcome_meta[1],
                 "source_outcomes_complete": outcome_meta[2]}
        if len(canonical) > len(card["concepts"]):
            card["concepts_omitted"] = len(canonical) - len(card["concepts"])
        cards.append(card)
    cards.sort(key=lambda k: k["run_id"])
    # every outward collection has an independent hard cap. Totals and explicit omission
    # counters describe the full validated snapshot, so a bounded response never masquerades as complete.
    result = {"n_runs": len(valid_capsules), "n_concepts": len(concepts),
               "concepts": concepts[:_MAX_OVERVIEW_CONCEPTS],
               "runs": cards[:_MAX_OVERVIEW_RUN_CARDS],
               **_capsule_source_summary(valid_capsules)}
    if len(concepts) > len(result["concepts"]):
        result["concepts_omitted"] = len(concepts) - len(result["concepts"])
    if len(cards) > len(result["runs"]):
        result["run_cards_omitted"] = len(cards) - len(result["runs"])
    return result, concepts
