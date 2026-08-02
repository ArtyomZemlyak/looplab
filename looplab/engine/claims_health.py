"""Claim SOURCE-ROW validation and read-health receipts — the leaf of the claims subsystem.

Split out of the 2,896-line `claims.py` god-module (doc 25 EM-01), which spanned six subsystems
whose module-private helpers referenced each other, so any change to one forced navigating all six.
This is the part everything else stands on and that stands on nothing above it: bounds, field sets,
row validators, the read-health segment/receipt shapes, and the `_safe_*` readers that turn an
unreadable store into an explicit UNKNOWN instead of a confident empty answer.

The direction is the point. A confident empty answer is the failure mode these validators exist to
prevent — "no claims oppose this" must never be indistinguishable from "the claim store could not be
read" — so the health layer may not depend on the governance ledger, the durable store, the
projections or the retrieval planner. It is imported by all four.

`claims.py` re-exports every name here (the `llm.py` / `agent.py` barrel pattern), so both spellings
resolve to the SAME objects and existing imports and monkeypatch seams are unaffected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from typing import Optional

from looplab.engine.governance_health import (
    GovernanceLedgerUnavailable,
    confirm_governance_durable,
    observed_path_missing,
    read_governance_rows,
    raise_governance_storage_unavailable,
    validate_action_ids,
    validate_local_revisions,
    validate_optional_text,
    validate_revision_fields,
)
from looplab.engine.memory import _CLAIM_STANCES, _NEGATIVE, normalize_statement
from looplab.trust.cross_run import (
    cross_run_identity_text,
    cross_run_text,
    sanitize_cross_run_projection,
)

_MAX_SOURCE_STATEMENT = 4000
# Shared with the governance ledger and the assessment projections: one bound on the metric
# token that identifies a decision, kept with the other bounds rather than in whichever
# section declared it first.
_MAX_DECISION_METRIC = 200
_MAX_SOURCE_ID = 500
_MAX_SOURCE_EVIDENCE = 256
_MAX_SOURCE_FINGERPRINT = 256
_MAX_CLAIM_PROJECTION_ITEMS = 64
_MAX_CONTEXT_CLAIMS = 64
_MAX_RETRIEVAL_HITS = 64
_MAX_RETRIEVAL_CORPUS = 4096
_RESEARCH_CLAIM_VERSION = 3
_RESEARCH_SOURCE_RECEIPT_VERSION = 1
_RESEARCH_SOURCE_RECEIPT_V2 = 2
_MAX_RESEARCH_CLAIMS_PER_RUN = 256
_MAX_RESEARCH_SOURCE_ITEMS = (1 << 31) - 1
_CLAIM_READ_HEALTH_VERSION = 1
_LESSON_OUTCOMES = frozenset((*_NEGATIVE, "supported", "noted", ""))
_RESEARCH_SOURCE_RECEIPT_ROW_FIELDS = frozenset((
    "v", "record_kind", "run_id", "task_id", "direction", "source_receipt",
))
_CLAIM_SOURCE_SEMANTIC_FIELDS = (
    "v", "record_kind", "run_id", "task_id", "direction", "statement", "metric",
    "metric_name", "metric_key", "objective_metric", "node_ids", "node_refs", "urls",
    "url_identities", "evidence_receipt", "verification",
    "verification_verdict", "verification_method", "verification_note", "source_receipt",
    "outcome", "claim_stance", "evidence", "fingerprint", "source", "role",
)
_RESEARCH_VERIFICATION_FIELDS = ("verdict", "method", "note")
_RESEARCH_SOURCE_RECEIPT_FIELDS = (
    "v", "claims_total", "claims_retained", "claims_omitted", "producer_complete",
    "claims_receipt_known", "evidence_complete",
)
_RESEARCH_EVIDENCE_RECEIPT_FIELDS = (
    "v", "node_refs_total", "node_refs_retained", "node_refs_omitted",
    "url_refs_total", "url_refs_retained", "url_refs_omitted", "complete",
)
_CLAIM_SOURCE_ROW_MAX_CHARS = 640_000
_CLAIM_SOURCE_ROW_MAX_TOTAL_ITEMS = 1_024


def _empty_claim_read_segment() -> dict:
    return {
        "read_complete": True,
        "rows_total": 0,
        "rows_retained": 0,
        "rows_quarantined": 0,
        "malformed_rows": 0,
        "invalid_rows": 0,
    }


def _empty_claim_read_health() -> dict:
    return {
        "v": _CLAIM_READ_HEALTH_VERSION,
        "receipt_known": True,
        "read_complete": True,
        "lessons": _empty_claim_read_segment(),
        "research": _empty_claim_read_segment(),
    }


def _safe_claim_read_segment(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or type(raw.get("read_complete")) is not bool:
        return None
    keys = ("rows_total", "rows_retained", "rows_quarantined", "malformed_rows", "invalid_rows")
    if any(type(raw.get(key)) is not int or not 0 <= raw[key] <= _MAX_RESEARCH_SOURCE_ITEMS
           for key in keys):
        return None
    out = {"read_complete": raw["read_complete"], **{key: raw[key] for key in keys}}
    consistent = (
        out["rows_quarantined"] == out["malformed_rows"] + out["invalid_rows"]
        and out["rows_total"] == out["rows_retained"] + out["rows_quarantined"]
        and out["read_complete"] == (out["rows_quarantined"] == 0)
    )
    return out if consistent else None


def _safe_claim_read_health(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("v") != _CLAIM_READ_HEALTH_VERSION:
        return None
    lessons = _safe_claim_read_segment(raw.get("lessons"))
    research = _safe_claim_read_segment(raw.get("research"))
    if (lessons is None or research is None or type(raw.get("read_complete")) is not bool
            or raw["read_complete"] != (lessons["read_complete"] and research["read_complete"])):
        return None
    return {
        "v": _CLAIM_READ_HEALTH_VERSION,
        "read_complete": raw["read_complete"],
        "lessons": lessons,
        "research": research,
    }


class _ClaimSourceRows(list):
    """List-compatible evidence snapshot carrying file/schema health through scope filters."""

    def __init__(self, rows=(), *, read_health: Optional[dict] = None):
        super().__init__(rows)
        self.read_health = _safe_claim_read_health(read_health) or _empty_claim_read_health()


def _claim_source_rows(rows, *, research: bool) -> _ClaimSourceRows:
    source = rows if isinstance(rows, (list, tuple)) else []
    valid = [row for row in source if _valid_claim_source_row(row, research=research)]
    inherited = _safe_claim_read_health(getattr(source, "read_health", None))
    if inherited is not None:
        # A scoped/filter projection keeps the physical denominator. If a caller mutates a carried snapshot
        # by appending a bad row, conservatively add that newly visible schema failure as well.
        local_invalid = len(source) - len(valid)
        if local_invalid:
            inherited = {
                **inherited,
                "lessons": dict(inherited["lessons"]),
                "research": dict(inherited["research"]),
            }
            key = "research" if research else "lessons"
            segment = inherited[key]
            segment["invalid_rows"] += local_invalid
            segment["rows_quarantined"] += local_invalid
            segment["rows_total"] += local_invalid
            segment["read_complete"] = False
            inherited["read_complete"] = False
        return _ClaimSourceRows(valid, read_health=inherited)

    key = "research" if research else "lessons"
    health = _empty_claim_read_health()
    invalid = len(source) - len(valid)
    health[key] = {
        "read_complete": invalid == 0,
        "rows_total": len(source),
        "rows_retained": len(valid),
        "rows_quarantined": invalid,
        "malformed_rows": 0,
        "invalid_rows": invalid,
    }
    health["read_complete"] = invalid == 0
    return _ClaimSourceRows(valid, read_health=health)


def _filter_claim_source_rows(rows, predicate, *, research: bool) -> _ClaimSourceRows:
    source = _claim_source_rows(rows, research=research)
    return _ClaimSourceRows(
        (row for row in source if predicate(row)), read_health=source.read_health)


def _claim_source_semantic_projection(row: dict) -> dict:
    """Exact fields consumed by claim identity, evidence, scope and producer-receipt logic."""
    out = {key: row[key] for key in _CLAIM_SOURCE_SEMANTIC_FIELDS if key in row}
    # nested v3 dictionaries are extensible, but their unknown keys must not consume the
    # sanitizer's item budget and push an authoritative field past the retained prefix. Select exact
    # contract keys before bounding/redacting, just as the top-level projection does.
    nested_fields = {
        "verification": _RESEARCH_VERIFICATION_FIELDS,
        "source_receipt": _RESEARCH_SOURCE_RECEIPT_FIELDS,
        "evidence_receipt": _RESEARCH_EVIDENCE_RECEIPT_FIELDS,
    }
    for field, keys in nested_fields.items():
        raw = out.get(field)
        if isinstance(raw, dict):
            out[field] = {key: raw[key] for key in keys if key in raw}
    raw_refs = out.get("node_refs")
    if isinstance(raw_refs, (list, tuple)):
        out["node_refs"] = [
            {key: ref[key] for key in ("node_id", "generation") if key in ref}
            for ref in raw_refs if isinstance(ref, dict)
        ]
    return out


def _claim_rows_snapshot_digest(rows, *, read_segment: dict) -> str:
    """Content identity for one validated/scoped source snapshot, not merely its row counts."""
    # Hash every row independently so a response/display cap cannot make a same-count rewrite outside its
    # first page invisible. Commit only fields consumed by claim/scope/producer logic; unrelated custom
    # extras must not make governance identity expensive or unstable.
    row_digests = []
    for row in rows:
        semantic = _claim_source_semantic_projection(row)
        semantic = sanitize_cross_run_projection(
            semantic, max_chars=_CLAIM_SOURCE_ROW_MAX_CHARS,
            max_items=_MAX_SOURCE_EVIDENCE,
            max_total_items=_CLAIM_SOURCE_ROW_MAX_TOTAL_ITEMS)
        encoded = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        )
        row_digests.append(hashlib.sha256(encoded.encode("utf-8")).hexdigest())
    raw = json.dumps(
        {"row_digests": row_digests, "read_health": {
            # Physical quarantine is global authority even for a scoped query. Valid rows outside the
            # requested scope, however, must not stale a scope-local governance digest merely by changing
            # the file-wide retained denominator.
            key: read_segment[key] for key in (
                "read_complete", "rows_quarantined", "malformed_rows", "invalid_rows")
        }},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_claim_source_path(path, *, research: bool) -> _ClaimSourceRows:
    """Read one durable evidence store without laundering malformed/schema-invalid rows into absence."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient_with_health
    p = Path(path)
    rows, raw_health = read_jsonl_lenient_with_health(
        p, loads=json.loads, dicts_only=True)
    valid = [row for row in rows if _valid_claim_source_row(row, research=research)]
    malformed = int(raw_health.get("malformed_lines", 0) or 0)
    invalid = int(raw_health.get("invalid_shape_lines", 0) or 0) + len(rows) - len(valid)
    quarantined = malformed + invalid
    key = "research" if research else "lessons"
    health = _empty_claim_read_health()
    health[key] = {
        "read_complete": quarantined == 0,
        "rows_total": int(raw_health.get("source_lines", 0) or 0),
        "rows_retained": len(valid),
        "rows_quarantined": quarantined,
        "malformed_rows": malformed,
        "invalid_rows": invalid,
    }
    health["read_complete"] = quarantined == 0
    return _ClaimSourceRows(valid, read_health=health)


def _valid_node_source(raw) -> bool:
    if raw is None:
        return True
    values = [raw] if isinstance(raw, int) and not isinstance(raw, bool) else raw
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_SOURCE_EVIDENCE:
        return False
    for value in values:
        # A node id is an INDEX into the run's node table and is therefore non-negative. A negative
        # int (or a signed numeric string) used to clear this completeness fence and go on to be
        # run-qualified into an authoritative-looking phantom ref like `run:-1` — a citation to a node
        # that cannot exist, in a row the health receipt still called complete.
        if type(value) is int:
            if value < 0:
                return False
            continue
        # claim source health is an authority signal, so a poisoned element cannot be
        # silently dropped by ``_node_ids`` while the surrounding row remains "complete". Numeric-string
        # compatibility stays bounded and exact; bool/float/container/arbitrary strings quarantine the row.
        if isinstance(value, str):
            text = value.strip()
            if text and len(text) <= 24 and text.lstrip("-").isdigit():
                try:
                    parsed = int(text)
                except (ValueError, OverflowError):
                    return False
                if parsed < 0:
                    return False          # same phantom ref, spelled as a string
                continue
        return False
    return True


def _indexable_research_claim(row) -> bool:
    """Defense-in-depth discriminator for rows that may contribute claim semantics.

    Validation remains the schema authority, but every assessment/index loop independently refuses a
    current-schema sentinel (or another kinded row) so a validator regression cannot turn a producer
    cardinality receipt into evidence. Unversioned/v0-v2 rows retain their historical empty-kind shape.
    """
    if not isinstance(row, dict):
        return False
    kind = row.get("record_kind")
    return kind in (None, "", "claim") and (
        row.get("v") != _RESEARCH_CLAIM_VERSION or kind == "claim")


def _valid_research_node_refs(raw, node_ids) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, list) or len(raw) > 8:
        return False
    cited = set(node_ids) if isinstance(node_ids, list) else set()
    return all(
        isinstance(ref, dict) and set(ref) == {"node_id", "generation"}
        and type(ref.get("node_id")) is int and ref["node_id"] >= 0
        and type(ref.get("generation")) is int and ref["generation"] >= 0
        and ref["node_id"] in cited
        for ref in raw
    )


def _valid_research_url_identities(raw, urls) -> bool:
    if raw is None:
        return True
    from looplab.core.source_identity import valid_source_identity
    return (isinstance(raw, list) and len(raw) <= 4 and len(raw) <= len(urls)
            and all(valid_source_identity(identity) for identity in raw))


def _valid_research_evidence_receipt(raw) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, dict) or set(raw) != set(_RESEARCH_EVIDENCE_RECEIPT_FIELDS):
        return False
    if raw.get("v") != 1 or type(raw.get("complete")) is not bool:
        return False
    values = [raw.get(key) for key in _RESEARCH_EVIDENCE_RECEIPT_FIELDS[1:-1]]
    if any(type(value) is not int or not 0 <= value <= _MAX_RESEARCH_SOURCE_ITEMS
           for value in values):
        return False
    return (
        raw["node_refs_total"] >= raw["node_refs_retained"]
        and raw["node_refs_retained"] <= 8
        and raw["node_refs_omitted"] == raw["node_refs_total"] - raw["node_refs_retained"]
        and raw["url_refs_total"] >= raw["url_refs_retained"]
        and raw["url_refs_retained"] <= 4
        and raw["url_refs_omitted"] == raw["url_refs_total"] - raw["url_refs_retained"]
        and raw["complete"] == (
            raw["node_refs_omitted"] == 0 and raw["url_refs_omitted"] == 0)
    )


def _valid_claim_source_row(row, *, research: bool) -> bool:
    """Conservative schema fence for persisted lesson/research evidence rows."""
    if not isinstance(row, dict):
        return False
    if research and row.get("record_kind") == "source_receipt":
        run_id, task_id, direction = row.get("run_id"), row.get("task_id"), row.get("direction")
        return (
            # a sentinel is an exact cardinality record, never an open claim envelope. If
            # statement/evidence/verification fields hitch a ride, assessment code must not be able to
            # index them while the same row advertises an authoritative retained count of zero.
            set(row) == _RESEARCH_SOURCE_RECEIPT_ROW_FIELDS
            and row.get("v") == _RESEARCH_CLAIM_VERSION
            and isinstance(run_id, str) and bool(run_id) and len(run_id) <= _MAX_SOURCE_ID
            and isinstance(task_id, str) and len(task_id) <= _MAX_SOURCE_ID
            and isinstance(direction, str) and direction in ("min", "max")
            and _research_source_receipt(row) is not None
        )
    if research:
        version = row.get("v")
        if version == _RESEARCH_CLAIM_VERSION:
            # v3 is exact, not a duck-typed extension point. Unknown kinds and malformed
            # producer receipts stay quarantined as raw rows; they cannot become claim evidence merely by
            # also carrying a plausible statement.
            if (row.get("record_kind") != "claim"
                    or _research_source_receipt(row) is None
                    or not isinstance(row.get("run_id"), str)
                    or not row["run_id"] or len(row["run_id"]) > _MAX_SOURCE_ID
                    or not isinstance(row.get("task_id"), str)
                    or len(row["task_id"]) > _MAX_SOURCE_ID
                    or not isinstance(row.get("direction"), str)
                    or row["direction"] not in ("min", "max")
                    or not isinstance(row.get("metric"), str)
                    or len(row["metric"]) > 200
                    or not isinstance(row.get("node_ids"), list)
                    or any(type(node_id) is not int or node_id < 0
                           for node_id in row["node_ids"])
                    or not _valid_research_node_refs(row.get("node_refs"), row["node_ids"])
                    or not isinstance(row.get("urls"), list)
                    or any(not isinstance(url, str) or len(url) > 2000 for url in row["urls"])
                    or not _valid_research_url_identities(row.get("url_identities"), row["urls"])
                    or not _valid_research_evidence_receipt(row.get("evidence_receipt"))
                    or not isinstance(row.get("verification"), dict)
                    or row["verification"].get("verdict") not in _RESEARCH_VERDICTS
                    or not isinstance(row["verification"].get("method"), str)
                    or len(row["verification"]["method"]) > 80
                    or not isinstance(row["verification"].get("note"), str)
                    or len(row["verification"]["note"]) > 400):
                return False
        elif version not in (None, 0, 1, 2) or row.get("record_kind") not in (None, ""):
            return False
    elif "v" in row or "record_kind" in row:
        # lessons.jsonl has an unversioned current claim-source shape. A versioned/kinded row belongs to an
        # unknown future contract and is retained by the mutable store but not interpreted by this reader.
        return False
    statement = row.get("statement")
    if not isinstance(statement, str) or not statement.strip() or len(statement) > _MAX_SOURCE_STATEMENT:
        return False
    for key in ("run_id", "task_id"):
        # Absence is the legacy unknown-scope discriminator. Explicit null/container/numeric scope is a
        # malformed semantic field, not permission to normalize the row into the shared portfolio scope.
        if key in row and (not isinstance(row[key], str) or len(row[key]) > _MAX_SOURCE_ID):
            return False
    if "direction" in row and (
            not isinstance(row["direction"], str)
            or row["direction"] not in ("", "min", "max")):
        return False
    if not _valid_node_source(row.get("node_ids" if research else "evidence")):
        return False
    fingerprint = row.get("fingerprint")
    if (fingerprint is not None
            and (not isinstance(fingerprint, (list, tuple))
                 or len(fingerprint) > _MAX_SOURCE_FINGERPRINT
                 or any(not isinstance(value, str) or len(value) > _MAX_SOURCE_ID
                        for value in fingerprint))):
        return False
    if research:
        urls = row.get("urls")
        if urls is not None and (not isinstance(urls, (list, tuple)) or len(urls) > 64):
            return False
        verification = row.get("verification")
        if verification is not None and not isinstance(verification, dict):
            return False
    else:
        # Missing/empty outcome remains the documented legacy-neutral form. Once present, every other
        # verdict and explicit stance must belong to the current durable vocabulary; otherwise downstream
        # string coercion could erase poisoned semantics while source_complete incorrectly stayed true.
        if ("outcome" in row
                and (not isinstance(row["outcome"], str)
                     or row["outcome"] not in _LESSON_OUTCOMES)):
            return False
        if ("claim_stance" in row
                and (not isinstance(row["claim_stance"], str)
                     or row["claim_stance"] not in _CLAIM_STANCES)):
            return False
        role = row.get("role")
        if role is not None and role not in ("", "researcher", "developer"):
            return False
    return True


def _valid_claim_source_rows(rows, *, research: bool) -> list[dict]:
    return _claim_source_rows(rows, research=research)


def _claim_text(value, maximum: int = 4000) -> str:
    return cross_run_text(value, max_chars=maximum, single_line=True, entropy=True).strip()


def _identity_text(value, maximum: int = 500) -> str:
    # Opaque run/task IDs are often hashes. Preserve their identity while still applying every known
    # credential pattern and stripping control/newline payloads.
    return cross_run_identity_text(value, max_chars=maximum).strip()


def _node_ids(raw) -> list:
    """Evidence node-id refs from a lesson's `evidence` or a claim's `node_ids`: ints kept as ints,
    numeric strings coerced, everything else dropped (a URL/source belongs in `sources`, not evidence)."""
    if isinstance(raw, bool) or raw is None:
        return []
    if isinstance(raw, int):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        return []
    out = []
    for x in raw:
        if isinstance(x, bool):
            continue
        if isinstance(x, int):
            if x >= 0:                    # a node id indexes the node table; `-1` is not a node
                out.append(x)
        elif (isinstance(x, str) and len(x.strip()) <= 24
              and x.strip().lstrip("-").isdigit()):
            try:
                parsed = int(x)
            except (ValueError, OverflowError):
                continue
            if parsed >= 0:
                out.append(parsed)
    return out


def _qualify_refs(run_id, node_ids) -> list[str]:
    """Run-QUALIFY evidence refs so (r1,node0) and (r2,node0) never collapse: a bare node id is run-local.
    "?" marks a ref whose run is unknown (e.g. a D8 claim without a run_id)."""
    r = _identity_text(run_id or "?", 500) or "?"
    return [f"{r}:{n}" for n in node_ids]


_RESEARCH_VERDICTS = frozenset(("supported", "unsupported", "unclear", "cited", "unverified"))


def _lesson_claim_stance(row: dict) -> str:
    """Map lesson evidence to the literal claim while preserving legacy rows exactly.

    New producers write an explicit stance. Presence with an invalid value fails closed to neutral;
    absence is the migration discriminator and retains the historical outcome projection.
    """
    if "claim_stance" in row:
        stance = str(row.get("claim_stance") or "")
        return stance if stance in _CLAIM_STANCES else "neutral"
    outcome = str(row.get("outcome") or "")
    if outcome == "supported":
        return "support"
    if outcome in _NEGATIVE:
        return "oppose"
    return "neutral"


def _research_verification(row: dict) -> tuple[str, str, str]:
    """Return ``(verdict, method, note)`` for one persisted D8 claim.

    Older rows had no verifier payload.  They are intentionally ``unverified`` rather than implicitly
    supported: a numeric citation proves only that the memo named a node, not that the node establishes the
    claim.  The nested shape is the durable v2 contract; top-level fields are accepted for migration.
    """
    raw = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    verdict = str(raw.get("verdict") or row.get("verification_verdict") or "unverified").lower()
    if verdict not in _RESEARCH_VERDICTS:
        verdict = "unverified"
    method = _claim_text(raw.get("method") or row.get("verification_method") or "", 80)
    note = _claim_text(raw.get("note") or row.get("verification_note") or "", 400)
    return verdict, method, note


def _research_source_receipt(row: dict) -> Optional[dict]:
    """Validate one v3 producer-cap receipt; legacy/malformed rows have unknown source coverage."""
    if type(row.get("v")) is not int or row.get("v") != _RESEARCH_CLAIM_VERSION:
        return None
    raw = row.get("source_receipt")
    if (not isinstance(raw, dict)
            or raw.get("v") not in (_RESEARCH_SOURCE_RECEIPT_VERSION,
                                    _RESEARCH_SOURCE_RECEIPT_V2)):
        return None
    total, retained, omitted = (
        raw.get("claims_total"), raw.get("claims_retained"), raw.get("claims_omitted"))
    complete = raw.get("producer_complete")
    if (type(total) is not int or type(retained) is not int or type(omitted) is not int
            or type(complete) is not bool
            or not 0 <= total <= _MAX_RESEARCH_SOURCE_ITEMS
            or not 0 <= retained <= _MAX_RESEARCH_CLAIMS_PER_RUN
            or total < retained or omitted != total - retained):
        return None
    if raw["v"] == _RESEARCH_SOURCE_RECEIPT_VERSION:
        if complete != (omitted == 0):
            return None
        return {
            "v": _RESEARCH_SOURCE_RECEIPT_VERSION,
            "claims_total": total,
            "claims_retained": retained,
            "claims_omitted": omitted,
            "producer_complete": complete,
        }
    claims_known = raw.get("claims_receipt_known")
    evidence_complete = raw.get("evidence_complete")
    if (type(claims_known) is not bool or type(evidence_complete) is not bool
            or complete != (claims_known and evidence_complete and omitted == 0)):
        return None
    return {
        "v": _RESEARCH_SOURCE_RECEIPT_V2,
        "claims_total": total,
        "claims_retained": retained,
        "claims_omitted": omitted,
        "producer_complete": complete,
        "claims_receipt_known": claims_known,
        "evidence_complete": evidence_complete,
    }


def _research_source_summary(rows) -> dict:
    """Aggregate per-run D8 producer receipts without treating a retained prefix as a full source.

    Unversioned rows supplied directly to the pure API are an explicit caller snapshot. Persisted
    unversioned rows are tagged ``v=0`` by ``load_research_claims`` and therefore remain UNKNOWN, just like
    durable v1/v2 rows whose former writer did not record its input cardinality.
    """
    validated = _claim_source_rows(rows, research=True)
    source = [row for row in validated if isinstance(row, dict)]
    read_health = validated.read_health["research"]
    groups: dict[str, list[dict]] = {}
    for row in source:
        run_id = _identity_text(row.get("run_id"), _MAX_SOURCE_ID)
        groups.setdefault(run_id or "<unknown-run>", []).append(row)

    partial = unknown = known_total = known_omitted = 0
    for members in groups.values():
        claim_members = [row for row in members if _indexable_research_claim(row)]
        # Direct pure-function callers already control the complete list they pass. Durable readers add a
        # version discriminator before this point, so absence cannot accidentally upgrade a legacy file.
        if all("v" not in row for row in members):
            known_total += len(claim_members)
            continue
        receipts = [_research_source_receipt(row) for row in members]
        if any(receipt is None for receipt in receipts):
            unknown += 1
            continue
        first = receipts[0]
        if (any(receipt != first for receipt in receipts[1:])
                or first["claims_retained"] != len(claim_members)):
            unknown += 1
            continue
        known_total += first["claims_total"]
        known_omitted += first["claims_omitted"]
        partial += int(first["producer_complete"] is not True)

    receipt_known = unknown == 0
    producer_complete = receipt_known and partial == 0
    read_complete = read_health["read_complete"]
    return {
        # this field is the policy gate consumed by claim verdicts/stewards. The producer-
        # prefixed fields intentionally leave an additive seam for store read-health (`quarantined_rows`,
        # `read_complete`): overall source completeness can later become their conjunction without changing
        # what this receipt says about the memo producer's 256-row cap.
        "source_complete": producer_complete and read_complete,
        "producer_receipt_known": receipt_known,
        "producer_complete": producer_complete,
        "producer_runs": len(groups),
        "producer_partial_runs": partial,
        "producer_unknown_runs": unknown,
        "producer_claims_total": known_total,
        "producer_claims_retained": sum(
            _indexable_research_claim(row) for row in source),
        "producer_claims_omitted": known_omitted,
        "read_health_v": _CLAIM_READ_HEALTH_VERSION,
        "read_complete": read_complete,
        "rows_total": read_health["rows_total"],
        "rows_retained": read_health["rows_retained"],
        "rows_quarantined": read_health["rows_quarantined"],
        "malformed_rows": read_health["malformed_rows"],
        "invalid_rows": read_health["invalid_rows"],
        "snapshot_digest": _claim_rows_snapshot_digest(
            validated, read_segment=read_health),
    }


def _safe_research_source_summary(raw) -> Optional[dict]:
    """Bound and validate a projected aggregate receipt before forwarding it to another boundary."""
    if not isinstance(raw, dict):
        return None
    base_bool_keys = ("source_complete", "producer_receipt_known", "producer_complete")
    base_int_keys = (
        "producer_runs", "producer_partial_runs", "producer_unknown_runs",
        "producer_claims_total", "producer_claims_retained", "producer_claims_omitted",
    )
    if any(type(raw.get(key)) is not bool for key in base_bool_keys):
        return None
    if any(type(raw.get(key)) is not int or not 0 <= raw[key] <= _MAX_RESEARCH_SOURCE_ITEMS
           for key in base_int_keys):
        return None
    out = {key: raw[key] for key in (*base_bool_keys, *base_int_keys)}
    known = out["producer_receipt_known"]
    base_consistent = (
        out["producer_partial_runs"] + out["producer_unknown_runs"] <= out["producer_runs"]
        # `known=true` and a non-zero unknown-run count is not a harmless diagnostic
        # mismatch: it would let a forged aggregate claim exact one-sided evidence while admitting an
        # unreadable producer receipt. The boolean and count are one invariant at every boundary.
        and known == (out["producer_unknown_runs"] == 0)
        and out["producer_complete"] == (known and out["producer_partial_runs"] == 0)
        and (not known or (
            out["producer_claims_total"] >= out["producer_claims_retained"]
            and out["producer_claims_omitted"]
            == out["producer_claims_total"] - out["producer_claims_retained"]))
    )
    if not base_consistent:
        return None

    extension_keys = (
        "read_health_v", "read_complete", "rows_total", "rows_retained",
        "rows_quarantined", "malformed_rows", "invalid_rows", "snapshot_digest",
    )
    present = [key in raw for key in extension_keys]
    if not any(present):
        # Backward-compatible producer-only receipt. Absence of the ENTIRE additive extension is one
        # coherent legacy contract; a partial extension below is rejected rather than default-filled.
        if out["source_complete"] != out["producer_complete"]:
            return None
        retained = out["producer_claims_retained"]
        return {
            **out,
            "read_health_v": 0,
            "read_complete": True,
            "rows_total": retained,
            "rows_retained": retained,
            "rows_quarantined": 0,
            "malformed_rows": 0,
            "invalid_rows": 0,
            "snapshot_digest": "",
        }
    if not all(present) or raw.get("read_health_v") != _CLAIM_READ_HEALTH_VERSION:
        return None
    if type(raw.get("read_complete")) is not bool:
        return None
    snapshot_digest = raw.get("snapshot_digest")
    if (not isinstance(snapshot_digest, str) or len(snapshot_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in snapshot_digest)):
        return None
    read_int_keys = ("rows_total", "rows_retained", "rows_quarantined", "malformed_rows", "invalid_rows")
    if any(type(raw.get(key)) is not int or not 0 <= raw[key] <= _MAX_RESEARCH_SOURCE_ITEMS
           for key in read_int_keys):
        return None
    out.update({"read_health_v": raw["read_health_v"], "read_complete": raw["read_complete"],
                **{key: raw[key] for key in read_int_keys},
                "snapshot_digest": snapshot_digest})
    consistent = (
        out["rows_quarantined"] == out["malformed_rows"] + out["invalid_rows"]
        and out["rows_total"] == out["rows_retained"] + out["rows_quarantined"]
        and out["read_complete"] == (out["rows_quarantined"] == 0)
        and out["source_complete"] == (out["producer_complete"] and out["read_complete"])
    )
    return out if consistent else None


def _claim_source_summary(lessons, research, *, research_source: Optional[dict] = None) -> dict:
    """Combine both physical/schema snapshots with the D8 producer-cap receipt."""
    lesson_rows = _claim_source_rows(lessons, research=False)
    research_rows = _claim_source_rows(research, research=True)
    lesson_read = lesson_rows.read_health["lessons"]
    research_read = research_rows.read_health["research"]
    research_source = (_safe_research_source_summary(research_source)
                       if research_source is not None else _research_source_summary(research_rows))
    if research_source is None:
        research_source = {
            **_research_source_summary(_ClaimSourceRows()),
            "source_complete": False,
            "producer_receipt_known": False,
            "producer_complete": False,
            "producer_runs": 1,
            "producer_unknown_runs": 1,
        }
    read_complete = lesson_read["read_complete"] and research_read["read_complete"]
    return {
        "v": _CLAIM_READ_HEALTH_VERSION,
        "receipt_known": True,
        # exact one-sided/absence claims cross BOTH mutable files and the D8 producer cap.
        # Poisoned rows remain excluded as evidence, but they cannot disappear from this authority bit.
        "source_complete": lesson_read["read_complete"] and research_source["source_complete"],
        "read_complete": read_complete,
        "research_source_complete": research_source["source_complete"],
        "lessons": dict(lesson_read),
        "research": dict(research_read),
        "snapshot_digest": hashlib.sha256(
            ("claims/v1\0"
             + _claim_rows_snapshot_digest(lesson_rows, read_segment=lesson_read)
             + "\0"
             + _claim_rows_snapshot_digest(research_rows, read_segment=research_read))
            .encode("utf-8")
        ).hexdigest(),
    }


def _safe_claim_source_summary(raw) -> Optional[dict]:
    if (not isinstance(raw, dict) or raw.get("v") != _CLAIM_READ_HEALTH_VERSION
            or type(raw.get("receipt_known")) is not bool
            or type(raw.get("source_complete")) is not bool
            or type(raw.get("read_complete")) is not bool
            or type(raw.get("research_source_complete")) is not bool):
        return None
    snapshot_digest = raw.get("snapshot_digest")
    digest_valid = (isinstance(snapshot_digest, str)
                    and ((raw["receipt_known"] is False and snapshot_digest == "")
                         or (len(snapshot_digest) == 64
                             and all(ch in "0123456789abcdef" for ch in snapshot_digest))))
    if not digest_valid:
        return None
    lessons = _safe_claim_read_segment(raw.get("lessons"))
    research = _safe_claim_read_segment(raw.get("research"))
    if lessons is None or research is None:
        return None
    read_complete = lessons["read_complete"] and research["read_complete"]
    consistent = ((not raw["receipt_known"] and not raw["source_complete"]
                   and not raw["read_complete"] and not raw["research_source_complete"])
                  or (raw["receipt_known"]
                      and raw["read_complete"] == read_complete
                      and (not raw["research_source_complete"] or research["read_complete"])
                      and raw["source_complete"]
                      == (lessons["read_complete"] and raw["research_source_complete"])))
    if not consistent:
        return None
    return {
        "v": _CLAIM_READ_HEALTH_VERSION,
        "receipt_known": raw["receipt_known"],
        "source_complete": raw["source_complete"],
        "read_complete": raw["read_complete"],
        "research_source_complete": raw["research_source_complete"],
        "lessons": lessons,
        "research": research,
        "snapshot_digest": snapshot_digest,
    }


def _unknown_claim_source_summary() -> dict:
    return {
        "v": _CLAIM_READ_HEALTH_VERSION,
        "receipt_known": False,
        "source_complete": False,
        "read_complete": False,
        "research_source_complete": False,
        "lessons": _empty_claim_read_segment(),
        "research": _empty_claim_read_segment(),
        "snapshot_digest": "",
    }


class _ClaimAssessmentRows(list):
    """Claim projection retaining aggregate source authority even when zero rows survive filters."""

    def __init__(self, rows=(), *, claim_source: Optional[dict] = None,
                 research_source: Optional[dict] = None):
        super().__init__(rows)
        self.claim_source = _safe_claim_source_summary(claim_source)
        self.research_source = _safe_research_source_summary(research_source)


def _filter_claim_assessments(rows, predicate) -> _ClaimAssessmentRows:
    source = rows if isinstance(rows, (list, tuple)) else []
    return _ClaimAssessmentRows(
        (row for row in source if predicate(row)),
        claim_source=getattr(source, "claim_source", None),
        research_source=getattr(source, "research_source", None),
    )


def _source_guarded_epistemic(support, oppose, claim_source: dict) -> str:
    state = _epistemic(support, oppose)
    # A missing lesson/research row or D8 tail may contain the other side. Preserve retained refs, but do not
    # emit either one-sided state from a lower-bound evidence source.
    return ("inconclusive" if state in ("supported", "refuted")
            and claim_source["source_complete"] is not True else state)


def _metric_identity(row: dict) -> str:
    """Best available metric *name* for structured identity (never a numeric score)."""
    for key in ("metric_name", "metric_key", "objective_metric", "metric"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return _identity_text(value, _MAX_DECISION_METRIC)
    fingerprint = row.get("fingerprint")
    if isinstance(fingerprint, (list, tuple)):
        for token in fingerprint:
            if isinstance(token, str) and token.casefold().startswith("metric:"):
                return _identity_text(token.split(":", 1)[1], _MAX_DECISION_METRIC)
    return ""


def _epistemic(support, oppose) -> str:
    """The evidence's current verdict on a claim. 'mixed' when both sides exist (a scoped disagreement,
    never newest-wins); 'inconclusive' when only neutral/unknown evidence remains — distinct from a
    supported/refuted claim (§21.20.1: absence is not failure)."""
    if support and oppose:
        return "mixed"
    if support:
        return "supported"
    if oppose:
        return "refuted"
    return "inconclusive"


def claim_evidence_digest(claim: dict) -> str:
    """Stable revision token for the evidence projection an operator actually reviewed.

    Governance metadata is deliberately excluded: ``expected_revision`` fences the decision ledger. This
    digest changes when proof, verification, provenance, or a live opposite-polarity assertion changes.
    """
    fields = (
        "claim_uid", "statement", "scope", "metric", "polarity", "epistemic", "support", "oppose",
        "unverified", "runs", "scopes", "sources", "verification", "contradicts", "research_source",
        "claim_source",
    )
    payload = {key: claim.get(key) for key in fields}
    research_source = _safe_research_source_summary(payload.get("research_source"))
    if research_source is not None:
        payload["research_source"] = {key: research_source[key] for key in (
            "source_complete", "producer_receipt_known", "producer_complete", "producer_runs",
            "producer_partial_runs", "producer_unknown_runs", "producer_claims_total",
            "producer_claims_retained", "producer_claims_omitted", "read_health_v", "read_complete",
            "rows_quarantined", "malformed_rows", "invalid_rows", "snapshot_digest",
        )}
    claim_source = _safe_claim_source_summary(payload.get("claim_source"))
    if claim_source is not None:
        payload["claim_source"] = {key: claim_source[key] for key in (
            "v", "receipt_known", "source_complete", "read_complete",
            "research_source_complete", "snapshot_digest",
        )}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _bounded_claim_projection(row: dict) -> dict:
    """Cap every nested collection after the full evidence digest/counts have been computed.

    Governance freshness continues to cover the complete evidence set; only the outward read-model is
    bounded. Explicit omission counts prevent a client from interpreting the visible prefix as complete.
    """
    out = dict(row)
    field_limits = {
        "support": (700, _MAX_CLAIM_PROJECTION_ITEMS),
        "oppose": (700, _MAX_CLAIM_PROJECTION_ITEMS),
        "unverified": (700, _MAX_CLAIM_PROJECTION_ITEMS),
        "runs": (_MAX_SOURCE_ID, _MAX_CLAIM_PROJECTION_ITEMS),
        "scopes": (_MAX_SOURCE_ID, _MAX_CLAIM_PROJECTION_ITEMS),
        "sources": (2000, 32),
        "verification": (120, 32),
        "contradicts": (_MAX_SOURCE_STATEMENT, 32),
        "merged_from": (_MAX_SOURCE_STATEMENT, 32),
    }
    omitted = {}
    for field, (text_limit, item_limit) in field_limits.items():
        raw = row.get(field)
        values = raw if isinstance(raw, (list, tuple)) else []
        projected = [value[:text_limit] for value in values[:item_limit]
                     if isinstance(value, str) and value]
        out[field] = projected
        hidden = len(values) - len(projected)
        if hidden:
            omitted[field] = hidden
    out["n_contradicts"] = len(row.get("contradicts") or []) \
        if isinstance(row.get("contradicts"), (list, tuple)) else 0
    decision = row.get("decision")
    if isinstance(decision, dict):
        text_fields = {
            "statement": _MAX_SOURCE_STATEMENT, "scope": _MAX_SOURCE_ID, "metric": 200,
            "decision": 20, "note": 4000, "by": 120, "at": 120, "action_id": 160,
            "evidence_digest": 80, "claim_uid": 80, "key": 160,
        }
        safe_decision = {key: value[:maximum] for key, maximum in text_fields.items()
                         if isinstance((value := decision.get(key)), str)}
        if isinstance(decision.get("revision"), int) and not isinstance(decision.get("revision"), bool):
            safe_decision["revision"] = max(0, decision["revision"])
        out["decision"] = safe_decision
    else:
        out["decision"] = None
    research_source = _safe_research_source_summary(row.get("research_source"))
    if research_source is None:
        out.pop("research_source", None)
    else:
        out["research_source"] = research_source
    claim_source = _safe_claim_source_summary(row.get("claim_source"))
    if claim_source is None:
        out.pop("claim_source", None)
    else:
        out["claim_source"] = claim_source
    if omitted:
        # per-field omission metadata is part of the projection contract; a hard nested cap
        # must never silently turn "64 shown of 3,000" into "there are 64".
        out["nested_omitted"] = omitted
    return out


