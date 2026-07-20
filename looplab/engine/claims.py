"""PART IV cross-run Step 4 (§21.20) — evidence-grounded CLAIM assessments.

A pure read-model that projects the ALREADY-SHIPPED memory into verifiable claims: a distilled lesson
already carries `{statement, outcome, evidence:[node_ids], run_id, task_id}` (a verdict + its grounding
nodes), and a D8 deep-research memo carries `claims:[{statement, node_ids, urls}]`. This module UNIFIES
those two shapes (it does not fork a third): it groups by normalized statement and records support vs
oppose evidence refs plus an epistemic state, so the loop/UI can ask "what does the accumulated evidence
suggest, and what contradicts it?" — the §21.20.5 claim idea in lean form.

Deliberately pure/deterministic and off any live path: no new store, no LLM, no I/O. The verdict→stance
mapping reuses the shipped lesson vocabulary (`memory._NEGATIVE` / "supported"); a "noted"/unknown verdict
is neutral (it takes no stance), exactly as on the lesson read/write paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from typing import Optional

from looplab.engine.memory import _CLAIM_STANCES, _NEGATIVE, normalize_statement
from looplab.trust.cross_run import (
    cross_run_identity_text,
    cross_run_text,
    sanitize_cross_run_projection,
)

_MAX_SOURCE_STATEMENT = 4000
_MAX_SOURCE_ID = 500
_MAX_SOURCE_EVIDENCE = 256
_MAX_SOURCE_FINGERPRINT = 256
_MAX_CLAIM_PROJECTION_ITEMS = 64
_MAX_CONTEXT_CLAIMS = 64
_MAX_RETRIEVAL_HITS = 64
_MAX_RETRIEVAL_CORPUS = 4096
_RESEARCH_CLAIM_VERSION = 3
_RESEARCH_SOURCE_RECEIPT_VERSION = 1
_MAX_RESEARCH_CLAIMS_PER_RUN = 256
_MAX_RESEARCH_SOURCE_ITEMS = (1 << 31) - 1
_CLAIM_READ_HEALTH_VERSION = 1
_LESSON_OUTCOMES = frozenset((*_NEGATIVE, "supported", "noted", ""))
_RESEARCH_SOURCE_RECEIPT_ROW_FIELDS = frozenset((
    "v", "record_kind", "run_id", "task_id", "direction", "source_receipt",
))
_CLAIM_SOURCE_SEMANTIC_FIELDS = (
    "v", "record_kind", "run_id", "task_id", "direction", "statement", "metric",
    "metric_name", "metric_key", "objective_metric", "node_ids", "urls", "verification",
    "verification_verdict", "verification_method", "verification_note", "source_receipt",
    "outcome", "claim_stance", "evidence", "fingerprint", "source", "role",
)
_RESEARCH_VERIFICATION_FIELDS = ("verdict", "method", "note")
_RESEARCH_SOURCE_RECEIPT_FIELDS = (
    "v", "claims_total", "claims_retained", "claims_omitted", "producer_complete",
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
    # CODEX AGENT: nested v3 dictionaries are extensible, but their unknown keys must not consume the
    # sanitizer's item budget and push an authoritative field past the retained prefix. Select exact
    # contract keys before bounding/redacting, just as the top-level projection does.
    nested_fields = {
        "verification": _RESEARCH_VERIFICATION_FIELDS,
        "source_receipt": _RESEARCH_SOURCE_RECEIPT_FIELDS,
    }
    for field, keys in nested_fields.items():
        raw = out.get(field)
        if isinstance(raw, dict):
            out[field] = {key: raw[key] for key in keys if key in raw}
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
        p, loads=json.loads, dicts_only=True) if p.exists() else ([], {
            "source_lines": 0, "malformed_lines": 0, "invalid_shape_lines": 0,
        })
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
        if type(value) is int:
            continue
        # CODEX AGENT: claim source health is an authority signal, so a poisoned element cannot be
        # silently dropped by ``_node_ids`` while the surrounding row remains "complete". Numeric-string
        # compatibility stays bounded and exact; bool/float/container/arbitrary strings quarantine the row.
        if isinstance(value, str):
            text = value.strip()
            if text and len(text) <= 24 and text.lstrip("-").isdigit():
                try:
                    int(text)
                except (ValueError, OverflowError):
                    return False
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


def _valid_claim_source_row(row, *, research: bool) -> bool:
    """Conservative schema fence for persisted lesson/research evidence rows."""
    if not isinstance(row, dict):
        return False
    if research and row.get("record_kind") == "source_receipt":
        run_id, task_id, direction = row.get("run_id"), row.get("task_id"), row.get("direction")
        return (
            # CODEX AGENT: a sentinel is an exact cardinality record, never an open claim envelope. If
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
            # CODEX AGENT: v3 is exact, not a duck-typed extension point. Unknown kinds and malformed
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
                    or any(type(node_id) is not int for node_id in row["node_ids"])
                    or not isinstance(row.get("urls"), list)
                    or any(not isinstance(url, str) or len(url) > 2000 for url in row["urls"])
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
            out.append(x)
        elif (isinstance(x, str) and len(x.strip()) <= 24
              and x.strip().lstrip("-").isdigit()):
            try:
                out.append(int(x))
            except (ValueError, OverflowError):
                continue
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
    if not isinstance(raw, dict) or raw.get("v") != _RESEARCH_SOURCE_RECEIPT_VERSION:
        return None
    total, retained, omitted = (
        raw.get("claims_total"), raw.get("claims_retained"), raw.get("claims_omitted"))
    complete = raw.get("producer_complete")
    if (type(total) is not int or type(retained) is not int or type(omitted) is not int
            or type(complete) is not bool
            or not 0 <= total <= _MAX_RESEARCH_SOURCE_ITEMS
            or not 0 <= retained <= _MAX_RESEARCH_CLAIMS_PER_RUN
            or total < retained or omitted != total - retained
            or complete != (omitted == 0)):
        return None
    return {
        "v": _RESEARCH_SOURCE_RECEIPT_VERSION,
        "claims_total": total,
        "claims_retained": retained,
        "claims_omitted": omitted,
        "producer_complete": complete,
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
        # CODEX AGENT: this field is the policy gate consumed by claim verdicts/stewards. The producer-
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
        # CODEX AGENT: `known=true` and a non-zero unknown-run count is not a harmless diagnostic
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
        # CODEX AGENT: exact one-sided/absence claims cross BOTH mutable files and the D8 producer cap.
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
    # emit either one-sided state from a lower-bound evidence source (CODEX AGENT).
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
        # CODEX AGENT: per-field omission metadata is part of the projection contract; a hard nested cap
        # must never silently turn "64 shown of 3,000" into "there are 64".
        out["nested_omitted"] = omitted
    return out


# --------------------------------------------------------------------------- #
# Operator claim DECISIONS (§22.4) — the ONLY write to cross-run MEANING an actor other than the engine
# may make. Append-only, keyed by normalized statement, overlaid on the machine-proposed assessment.
# --------------------------------------------------------------------------- #

CLAIM_DECISIONS = ("ratified", "rejected", "pinned")
CLAIM_DECISION_ACTIONS = CLAIM_DECISIONS + ("clear",)

_MAX_DECISION_STATEMENT = 4000
_MAX_DECISION_SCOPE = 500
_MAX_DECISION_METRIC = 200
_MAX_DECISION_NOTE = 4000
_MAX_DECISION_ACTOR = 120
_MAX_DECISION_AT = 120
_MAX_DECISION_ACTION_ID = 160
_MAX_EVIDENCE_DIGEST = 80


class ClaimDecisionConflict(ValueError):
    """Optimistic-concurrency conflict on the append-only claim-governance ledger."""

    def __init__(self, expected: int, current: int):
        super().__init__(f"claim governance revision conflict: expected {expected}, current {current}")
        self.expected_revision = expected
        self.current_revision = current


class ClaimDecisionIdempotencyConflict(ValueError):
    """An ``action_id`` was reused with a different semantic decision payload."""


def _bounded(value, name: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise ValueError(f"empty {name}")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if name in {"scope", "metric", "action_id", "at", "evidence_digest"}:
        return cross_run_identity_text(text, max_chars=maximum).strip()
    return cross_run_text(
        text, max_chars=maximum, single_line=True, entropy=True)


def _decision_payload(row: dict) -> tuple:
    """Semantic request identity for ``action_id`` replay.

    Actor and timestamp are receipt metadata: a transport retry may be served after the deployment's
    operator label changes, but it must still return the original durable receipt instead of conflicting.
    """
    return (
        _claim_text(row.get("statement"), _MAX_DECISION_STATEMENT),
        _identity_text(row.get("scope"), _MAX_DECISION_SCOPE),
        _identity_text(row.get("metric"), _MAX_DECISION_METRIC),
        str(row.get("decision") or ""),
        _claim_text(row.get("note"), _MAX_DECISION_NOTE),
        _identity_text(row.get("evidence_digest"), _MAX_EVIDENCE_DIGEST),
    )


def _logical_decision_rows(rows) -> list[dict]:
    """Quarantine malformed/id-colliding rows and assign one monotonic logical revision per action."""
    logical: list[dict] = []
    actions: dict[str, tuple] = {}
    for raw in rows or []:
        if not isinstance(raw, dict) or raw.get("decision") not in CLAIM_DECISION_ACTIONS:
            continue
        action_id = str(raw.get("action_id") or "")
        if action_id:
            if action_id in actions:
                # Exact duplicate or collision: either way the repeated physical row is not a new action.
                continue
            actions[action_id] = _decision_payload(raw)
        logical.append({**raw, "revision": len(logical) + 1})
    return logical


def claim_governance_revision(memory_dir) -> int:
    """Current logical claim-governance revision; valid legacy rows count in file order."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return 0
    path = Path(memory_dir) / "claim_decisions.jsonl"
    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
    return len(_logical_decision_rows(rows))


def record_claim_decision(memory_dir, *, statement: str, decision: str, note: str = "",
                          by: str = "operator", at: str = "", scope: str = "", metric: str = "",
                          expected_revision: Optional[int] = None, action_id: str = "",
                          evidence_digest: str = "", validate: Optional[Callable[[], None]] = None,
                          validate_evidence: Optional[Callable[[list[dict]], None]] = None) -> dict:
    """Persist an OPERATOR verdict on a claim (ratify / reject / pin). Append-only JSONL, keyed BOTH by the
    legacy `normalize_statement` (so the lean projection still overlays) AND by a structured `claim_uid`
    (scope+polarity-precise, so a decision in task A never reaches a same-worded claim in task B — CODEX).
    `scope` (task id) / `metric` qualify the structured key. This is the §22.4 governance write — agents
    never call it. Returns the record. Durable locked+fsynced append; raises on an invalid decision or
    missing memory dir (a real operator error)."""
    from pathlib import Path

    if decision not in CLAIM_DECISION_ACTIONS:
        raise ValueError(f"decision must be one of {CLAIM_DECISION_ACTIONS}, got {decision!r}")
    if not memory_dir:
        raise ValueError("no memory_dir")
    # Reject oversized identity fields instead of truncating them: the exact persisted statement/scope/metric
    # must always recompute the same UID after restart. The 4000 statement cap matches persisted D8 claims.
    s = _bounded(statement, "statement", _MAX_DECISION_STATEMENT, required=True).strip()
    sc = _bounded(scope, "scope", _MAX_DECISION_SCOPE)
    mt = _bounded(metric, "metric", _MAX_DECISION_METRIC)
    aid = _bounded(action_id, "action_id", _MAX_DECISION_ACTION_ID).strip()
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    rec = {"statement": s, "key": normalize_statement(s), "claim_key_version": CLAIM_KEY_VERSION,
           "claim_uid": claim_uid(s, scope=sc, metric=mt), "scope": sc, "metric": mt,
           "decision": decision, "note": _bounded(note, "note", _MAX_DECISION_NOTE),
           "by": _bounded(by or "operator", "by", _MAX_DECISION_ACTOR),
           "at": _bounded(at, "at", _MAX_DECISION_AT)}
    digest = _bounded(evidence_digest, "evidence_digest", _MAX_EVIDENCE_DIGEST).strip()
    if digest:
        rec["evidence_digest"] = digest
    if aid:
        rec["action_id"] = aid
    path = Path(memory_dir) / "claim_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from looplab.core.atomicio import best_effort_fsync
    from looplab.events.eventstore import _interprocess_lock, read_jsonl_lenient
    # Idempotency lookup, revision CAS, allocation and append are one critical section. A
    # pre-lock check lets two UI writers both accept revision N and silently create divergent policy.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
        logical = _logical_decision_rows(rows)
        if aid:
            existing = next((r for r in logical
                             if _identity_text(r.get("action_id"), _MAX_DECISION_ACTION_ID) == aid), None)
            if existing is not None:
                if _decision_payload(existing) == _decision_payload(rec):
                    return sanitize_cross_run_projection(
                        existing, max_chars=16_000, max_items=64, max_total_items=256)
                raise ClaimDecisionIdempotencyConflict(
                    f"action_id {aid!r} was already used for a different claim decision")
        current = len(logical)
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ValueError("expected_revision must be an integer")
            if expected_revision != current:
                raise ClaimDecisionConflict(expected_revision, current)
        from contextlib import nullcontext
        evidence_context = (
            locked_claim_evidence_snapshot(memory_dir, structured=True)
            if validate_evidence is not None else nullcontext(None)
        )
        # CODEX AGENT: the evidence locks remain held through validation AND durable decision append. A
        # lesson/research rewrite cannot slip between evidence_digest comparison and governance commit.
        with evidence_context as evidence_snapshot:
            if validate is not None:
                validate()
            if validate_evidence is not None:
                validate_evidence(evidence_snapshot)
            stored = {**rec, "revision": current + 1}
            separator = ""
            if path.exists() and path.stat().st_size:
                with open(path, "rb") as existing:
                    existing.seek(-1, 2)
                    if existing.read(1) not in (b"\n", b"\r"):
                        # Preserve the torn forensic fragment but isolate the acknowledged valid row.
                        separator = "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(separator + json.dumps(stored) + "\n")
                f.flush()
                best_effort_fsync(f.fileno())
            return stored


def _global_key(legacy_key: str) -> str:
    """A DISTINCT index (in the same decisions dict) for the last SCOPE-LESS decision on a statement, so
    a later scoped decision that overwrites the plain legacy key can't hide the portfolio-wide verdict
    from the structured fallback. The control-char prefix won't collide with a claim_uid ("clm_"+hex) or,
    in practice, a normalize_statement key — the only way to collide is a statement literally beginning
    with a NUL byte, which argv, LLM text and engine-written JSON logs never carry. The dict is only ever
    read via `.get(key)`, never iterated, so the extra keys are safe."""
    return "\x00global\x00" + legacy_key


def _scoped_key(legacy_key: str, scope: str) -> str:
    """A lean-projection index for a scope-only decision.

    The structured UID remains authoritative.  This secondary key lets the default statement projection
    retrieve an exact task verdict without putting scoped policy back at the shared legacy key, where the
    latest task would overwrite every earlier task's decision.
    """
    return "\x00scope\x00" + str(scope) + "\x00" + legacy_key


def load_claim_decisions(memory_dir) -> dict:
    """Replay current decisions into safe global and structured namespaces.

    UIDs are recomputed with the current claim-key version, so durable v1 rows migrate on read. A scoped or
    metric-qualified row is indexed ONLY by its structured UID: it must never overwrite the global legacy
    statement key. Unscoped/unqualified rows remain the fallback for every scope. ``clear`` tombstones only
    the namespace it addresses. Last write wins within each exact namespace.
    """
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    if not path.exists():
        return {}
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    out: dict = {}
    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
    for r in _logical_decision_rows(rows):
        statement = _claim_text(r.get("statement"), _MAX_DECISION_STATEMENT)
        scope = _identity_text(r.get("scope"), _MAX_DECISION_SCOPE)
        metric = _identity_text(r.get("metric"), _MAX_DECISION_METRIC)
        k = normalize_statement(statement) if statement else str(r.get("key") or "")
        # A legacy scoped row without its statement cannot be migrated safely. Never fall back to its old UID:
        # that would silently replay a v1 token-set collision under the v2 role-aware contract.
        uid = claim_uid(statement, scope=scope, metric=metric) if statement else ""
        # Legacy decision rows predate always-on redaction. Keep their governance identity/revision but
        # never copy a nested note/actor/action payload back into an agent or HTTP projection verbatim.
        current = sanitize_cross_run_projection(
            {**r, "statement": statement, "scope": scope, "metric": metric,
             "claim_uid": uid, "claim_key_version": CLAIM_KEY_VERSION},
            max_chars=16_000, max_items=64, max_total_items=256)
        keys = ([uid] if uid else [])
        if k and not scope and not metric:
            # Retain a distinct portfolio-wide fallback as well as the legacy lean key. A
            # caller may merge overlays that place a scoped decision at the plain key; that must not erase
            # the durable global verdict for every other scope.
            keys.extend((k, _global_key(k)))
        elif k and scope and not metric:
            keys.append(_scoped_key(k, scope))
        # One semantic UID may have several historical display spellings. Retire every index that points
        # at the same namespace before applying its newest row, so ``clear`` cannot be bypassed through an
        # older legacy statement key.
        if uid:
            for old_key, old in list(out.items()):
                if str(old.get("claim_uid") or "") == uid:
                    out.pop(old_key, None)
        for key in keys:
            if r.get("decision") == "clear":
                out.pop(key, None)
            else:
                out[key] = current
    return out


def _string_list(raw, *, maximum: int, item_maximum: int) -> list[str]:
    """Bounded JSON-list normalization; strings are scalar values, never character iterables."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for value in raw[:maximum]:
        if isinstance(value, str):
            clean = _claim_text(value, item_maximum)
            if clean:
                out.append(clean)
    return out


# --------------------------------------------------------------------------- #
# D8 research claims persisted cross-run (§21.20 / CR1b) — so a deep-research memo's evidence-backed
# claims survive their run and can CONTEST/support lesson verdicts (contested is otherwise unreachable
# from newest-verdict-wins lessons alone). Written at finalize; read by the claim assessments callers.
# --------------------------------------------------------------------------- #

def record_research_claims(memory_dir, *, run_id: str, task_id: str, claims,
                           direction: str) -> int:
    """Upsert (by run_id) a run's D8 research claims into `research_claims.jsonl`. Each row:
    {run_id, task_id, statement, node_ids, urls, source_receipt}. Append-with-replace so a re-run doesn't
    double-count. Returns how many rows were written. Best-effort atomicity via the shared whole-file writer."""
    from pathlib import Path

    from looplab.events.eventstore import (
        _interprocess_lock, replace_jsonl_rows_atomic_preserving_quarantine,
    )
    if not memory_dir:
        return 0
    rid = _identity_text(run_id, 500)
    if not rid:
        return 0
    rows = []
    direction = str(direction or "")
    if direction not in ("min", "max"):
        # Current v3 identity is exact. An orientation-free record would be permanently unusable for live
        # scope and indistinguishable from a malformed writer, so refuse before replacing any prior rows.
        return 0
    source = claims if isinstance(claims, (list, tuple)) else []
    source_total = len(source) + int(claims is not None and not isinstance(claims, (list, tuple)))
    # CODEX AGENT: select the first bounded set of VALID claims rather than slicing the raw list first. A
    # malformed prefix must not hide a valid opposition row later in the memo, and every skipped/capped input
    # remains visible in the repeated per-run receipt below.
    for c in source:
        if len(rows) >= _MAX_RESEARCH_CLAIMS_PER_RUN:
            break
        stmt = _claim_text(c.get("statement") if isinstance(c, dict) else "", 4000)
        if not stmt:
            continue
        verdict, method, note = _research_verification(c)
        rows.append({"v": _RESEARCH_CLAIM_VERSION, "record_kind": "claim", "run_id": rid,
                     "task_id": _identity_text(task_id, 500),
                      "direction": direction,
                      "statement": stmt,
                      "metric": _metric_identity(c),
                      "node_ids": _node_ids(c.get("node_ids"))[:64],
                      "urls": _string_list(c.get("urls"), maximum=32, item_maximum=2000),
                      "verification": {"verdict": verdict, "method": method, "note": note}})
    claim_count = len(rows)
    receipt = {
        "v": _RESEARCH_SOURCE_RECEIPT_VERSION,
        "claims_total": source_total,
        "claims_retained": claim_count,
        "claims_omitted": source_total - claim_count,
        "producer_complete": source_total == claim_count,
    }
    for row in rows:
        row["source_receipt"] = receipt
    if claim_count == 0:
        # Every explicitly processed empty snapshot needs a durable denominator too. Otherwise a successful
        # empty extraction is indistinguishable from a run that never produced D8 at all, and a same-run
        # refresh can erase its only receipt. This sentinel participates in completeness only; assessment/
        # index loops ignore it because it has no statement (CODEX AGENT).
        rows.append({
            "v": _RESEARCH_CLAIM_VERSION,
            "record_kind": "source_receipt",
            "run_id": rid,
            "task_id": _identity_text(task_id, 500),
            "direction": direction,
            "source_receipt": receipt,
        })
    path = Path(memory_dir) / "research_claims.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the same interprocess lock the case/capsule/decision sidecar stores use — and RE-READ inside it —
    # so concurrent runs survive. Raw-line preservation additionally keeps unreadable/future records visible
    # to store-health readers instead of laundering quarantine into an apparently complete file.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        replace_jsonl_rows_atomic_preserving_quarantine(
            path,
            rows,
            # CODEX AGENT: only a fully understood current-schema sibling is an upsert target. A future,
            # legacy, or malformed row with the same apparent run_id is quarantine evidence, not permission
            # to erase bytes and falsely restore source_complete=true.
            replace_if=lambda row: (
                row.get("v") == _RESEARCH_CLAIM_VERSION
                and row.get("record_kind") in ("claim", "source_receipt")
                and _valid_claim_source_row(row, research=True)
                and _research_source_receipt(row) is not None
                and _identity_text(row.get("run_id"), _MAX_SOURCE_ID) == rid
            ),
            loads=json.loads,
            dumps=json.dumps,
        )
    return claim_count


def load_research_claims(memory_dir) -> list[dict]:
    """Persisted D8 claims plus non-indexed processed-empty/all-invalid receipt sentinels. [] when none."""
    from pathlib import Path

    if not memory_dir:
        return _ClaimSourceRows()
    path = Path(memory_dir) / "research_claims.jsonl"
    rows = _load_claim_source_path(path, research=True)
    projected = []
    for row in rows:
        # Keep every schema-bounded field consumed by claim identity, evidence and source digest while
        # dropping unrelated legacy extensions before they can exhaust the redaction budget. Outward claim
        # projections remain capped separately; this internal snapshot must retain an allowed 65th..256th
        # reference so a tail rewrite changes governance identity instead of disappearing (CODEX AGENT).
        durable = _claim_source_semantic_projection(row)
        # Missing version in a persisted file is not the direct pure-API snapshot compatibility case.
        durable.setdefault("v", 0)
        projected.append(sanitize_cross_run_projection(
            durable, max_chars=_CLAIM_SOURCE_ROW_MAX_CHARS,
            max_items=_MAX_SOURCE_EVIDENCE,
            max_total_items=_CLAIM_SOURCE_ROW_MAX_TOTAL_ITEMS))
    return _ClaimSourceRows(projected, read_health=rows.read_health)


def load_claim_lessons(memory_dir) -> list[dict]:
    """Claim-compatible lesson rows with physical/schema read health attached to the snapshot."""
    from pathlib import Path

    if not memory_dir:
        return _ClaimSourceRows()
    return _load_claim_source_path(Path(memory_dir) / "lessons.jsonl", research=False)


def locked_claim_evidence_snapshot(memory_dir, *, structured: bool = True):
    """Context manager yielding one cross-file evidence snapshot locked until its caller commits.

    This is intentionally separate from ordinary read projections: governance needs lessons.jsonl and
    research_claims.jsonl to stay unchanged from evidence-digest validation through decision append.
    """
    from contextlib import ExitStack, contextmanager
    from pathlib import Path

    from looplab.events.eventstore import _interprocess_lock

    @contextmanager
    def _snapshot():
        base = Path(memory_dir)
        paths = sorted(
            (base / "lessons.jsonl", base / "research_claims.jsonl"), key=lambda p: str(p))
        with ExitStack() as stack:
            for source_path in paths:
                stack.enter_context(_interprocess_lock(
                    Path(str(source_path) + ".lock"), required=True))
            lessons = load_claim_lessons(base)
            research = load_research_claims(base)
            # The decision overlay is read while record_claim_decision owns its ledger lock. Its maturity
            # does not enter evidence_digest, but is required for scope/global-clear target diagnostics.
            decisions = load_claim_decisions(base)
            assessments = claim_assessments(
                lessons, research_claims=research, decisions=decisions, structured=structured)
            assessments.lessons_snapshot = lessons
            assessments.research_claims_snapshot = research
            assessments.decisions_snapshot = decisions
            yield assessments

    return _snapshot()


def claims_for_memory(memory_dir, *, lessons=None, research_claims=None, decisions=None,
                      scope_task: str = "", fuzzy: bool = False,
                      structured: bool = False) -> list[dict]:
    """Convenience: `claim_assessments` over a memory dir — lessons.jsonl (or a pre-filtered `lessons`) +
    the persisted D8 research claims + the operator-decision overlay. One call so every read path applies
    research claims AND decisions consistently. `fuzzy` (opt-in) merges paraphrased claims (CR1b);
    `structured` (opt-in) uses the scope+polarity-safe structured claim key (the full CR); `scope_task`
    filters the D8 research claims to the bound task so a task-scoped caller does not re-read another task's
    research claims (mega-review) — the decisions overlay is applied scope-safely by `claim_assessments`."""
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    research = _valid_claim_source_rows(research, research=True)
    if scope_task:
        wanted = str(scope_task)
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    dec = load_claim_decisions(memory_dir) if decisions is None else decisions
    return claim_assessments(lessons, research_claims=research, decisions=dec,
                             fuzzy=fuzzy, structured=structured)


def atlas_for_memory(memory_dir, *, lessons=None, capsules=None, research_claims=None,
                     decisions=None, scope_task: str = "", max_items: int = 8,
                     structured: bool = False) -> dict:
    """Convenience: `portfolio_atlas` over a memory dir with EVERY overlay loaded — lessons + D8 research
    claims + operator decisions + concept aliases + splits. One call so every atlas surface is consistent.
    `structured` keeps the claim projection consistent with the researcher advisory; `scope_task` filters
    the D8 research claims to the bound task so a task-scoped caller does not surface another task's
    claims/contradictions (mega-review)."""
    from pathlib import Path

    from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
    from looplab.engine.memory import (ConceptCapsuleStore, _dedup_valid_capsules,
                                       _filter_capsule_rows)
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    capsule_source = capsules if isinstance(capsules, (list, tuple)) else []
    capsules = _dedup_valid_capsules(capsule_source)
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    research = _valid_claim_source_rows(research, research=True)
    if scope_task:
        wanted = str(scope_task)
        # Scope is an access boundary across every joined store, not just D8. Filtering only
        # research rows still leaked other tasks through lessons and concept capsules in the same response.
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        capsules = _filter_capsule_rows(
            capsules, lambda r: str(r.get("task_id") or "") == wanted)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    return portfolio_atlas(lessons, capsules, max_items=max_items,
                           decisions=(load_claim_decisions(memory_dir) if decisions is None else decisions),
                           research_claims=research,
                           aliases=load_concept_aliases(memory_dir),
                           splits=load_concept_splits(memory_dir), structured=structured)


_CLAIM_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _stmt_tokens(s: str) -> frozenset:
    return frozenset(w for w in _CLAIM_WORD.findall((s or "").casefold()) if len(w) > 2)


def _fuzzy_merge_claims(claims: list[dict], *, threshold: float = 0.6) -> list[dict]:
    """Conservative opt-in paraphrase projection.

    Candidates must share scope, semantic polarity and governance maturity, and every member must clear the
    threshold (complete-link). A bounded token index avoids all-pairs and single-link bridge collapse.
    """
    n = len(claims)
    if n <= 1:
        return claims
    from looplab.engine.claim_key import claim_signature
    toks = [_stmt_tokens(c["statement"]) for c in claims]
    meta = [(tuple(c.get("scopes") or []), claim_signature(c["statement"])["polarity"],
             str(c.get("maturity") or "machine-proposed")) for c in claims]
    groups: list[list[int]] = []
    token_groups: dict[str, set[int]] = {}
    for i, token_set in enumerate(toks):
        candidates = sorted({gid for token in token_set for gid in token_groups.get(token, ())})[:64]
        chosen = None
        for gid in candidates:
            members = groups[gid]
            if len(members) >= 64 or any(meta[j] != meta[i] for j in members):
                continue
            complete = True
            for j in members:
                union, inter = token_set | toks[j], token_set & toks[j]
                if not inter or len(inter) / len(union) < threshold:
                    complete = False
                    break
            if complete:
                chosen = gid
                break
        if chosen is None:
            chosen = len(groups)
            groups.append([])
        groups[chosen].append(i)
        for token in token_set:
            token_groups.setdefault(token, set()).add(chosen)

    out = []
    for idxs in groups:
        members = [claims[i] for i in idxs]
        if len(members) == 1:
            out.append(members[0])
            continue
        sup = sorted({r for m in members for r in m["support"]})
        opp = sorted({r for m in members for r in m["oppose"]})
        unverified = sorted({r for m in members for r in m.get("unverified", [])})
        rep = max(members, key=lambda m: (m["n_support"] + m["n_oppose"], m["statement"]))
        mat = members[0].get("maturity", "machine-proposed")
        research_source = (_safe_research_source_summary(members[0].get("research_source"))
                           or _research_source_summary([]))
        claim_source = (_safe_claim_source_summary(members[0].get("claim_source"))
                        or _claim_source_summary([], [], research_source=research_source))
        out.append({
            "statement": rep["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source), "maturity": mat,
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted({r for m in members for r in m["runs"]}),
            "scopes": sorted({r for m in members for r in m["scopes"]}),
            "sources": sorted({s for m in members for s in m.get("sources", [])}),
            "verification": sorted({v for m in members for v in m.get("verification", [])}),
            "decision": members[0].get("decision"),
            "merged_from": sorted(m["statement"] for m in members),
            "research_source": research_source,
            "claim_source": claim_source,
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return out


def _structured_assessments(lessons, research_claims, decisions, *,
                            research_source: Optional[dict] = None,
                            claim_source: Optional[dict] = None) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection (full CR of the lean fuzzy merge). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature, claim_uid
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = (_safe_research_source_summary(research_source)
                       if research_source is not None else _research_source_summary(research_claims))
    if research_source is None:
        research_source = _research_source_summary(research_claims)
    claim_source = (_safe_claim_source_summary(claim_source)
                    if claim_source is not None else _claim_source_summary(
                        lessons, research_claims, research_source=research_source))
    if claim_source is None:
        claim_source = _claim_source_summary(
            lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    groups: dict[str, dict] = {}

    def _grp(statement, scope, metric=""):
        s = _claim_text(statement)
        if not s:
            return None
        sig = claim_signature(
            s, scope=_identity_text(scope, _MAX_DECISION_SCOPE),
            metric=_identity_text(metric, _MAX_DECISION_METRIC))
        if sig["polarity"] == 0:                     # no subject content -> not a claim
            return None
        g = groups.get(sig["merge_key"])
        if g is None:
            g = groups[sig["merge_key"]] = {
                "uid": sig["uid"], "contra_key": sig["contra_key"], "polarity": sig["polarity"],
                "scope": sig["scope"], "metric": sig["metric"],
                "support": set(), "oppose": set(), "unverified": set(),
                "runs": set(), "scopes": set(), "sources": set(), "verification": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    for lz in lessons or []:
        g = _grp(lz.get("statement"), lz.get("task_id"), _metric_identity(lz))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        g["_ev"][_claim_text(lz.get("statement"))] += len(refs)

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _grp(rc.get("statement"), rc.get("task_id"), _metric_identity(rc))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))  # D8 registers run/scope now (CODEX)
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            # unsupported/unclear/cited/legacy-unverified evidence is not counter-evidence; it simply has
            # not established the claim.  Keep the refs drillable without promoting them to support.
            g["unverified"].update(refs)
        g["_ev"][_claim_text(rc.get("statement"))] += len(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}

    def _decision_for(g: dict, rep: str):
        overlay = decisions
        candidates = [g["uid"], claim_uid(rep, scope=g["scope"], metric=g["metric"])]
        if g["metric"]:
            candidates.append(claim_uid(rep, scope=g["scope"], metric=""))
        if g["metric"]:
            candidates.append(claim_uid(rep, scope="", metric=g["metric"]))
        candidates.append(claim_uid(rep, scope="", metric=""))
        seen = set()
        for uid in candidates:
            if uid and uid not in seen and isinstance(overlay.get(uid), dict):
                return overlay[uid]
            seen.add(uid)
        legacy_key = normalize_statement(rep)
        legacy = overlay.get(legacy_key)
        if (isinstance(legacy, dict) and not str(legacy.get("scope") or "")
                and not str(legacy.get("metric") or "")):
            return legacy
        global_legacy = overlay.get(_global_key(legacy_key))
        if (isinstance(global_legacy, dict) and not str(global_legacy.get("scope") or "")
                and not str(global_legacy.get("metric") or "")):
            return global_legacy
        return None

    prepared = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        decision = _decision_for(g, rep)
        if decision is not None:
            decision = sanitize_cross_run_projection(
                decision, max_chars=16_000, max_items=64, max_total_items=256)
        prepared.append({"group": g, "statement": rep, "support": sup, "oppose": opp,
                         "unverified": unverified, "decision": decision,
                         "maturity": _dec.get((decision or {}).get("decision"), "machine-proposed")})

    # Keep a governance-independent contradiction map for the evidence digest. The live projection below
    # may hide a rejected opposite, but rejecting it must not make the reviewed proof revision change by
    # itself; only source evidence should age a decision.
    raw_contra: dict[str, dict[int, list]] = {}
    contra: dict[str, dict[int, list]] = {}
    for item in prepared:
        if item["support"]:
            g = item["group"]
            raw_contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)
        if item["maturity"] != "operator-rejected" and item["support"]:
            contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)

    out = []
    for item in prepared:
        g, rep = item["group"], item["statement"]
        sup, opp, unverified = item["support"], item["oppose"], item["unverified"]
        opposites = ([] if item["maturity"] == "operator-rejected" else
                     [og for pol, gs in contra.get(g["contra_key"], {}).items() if pol != g["polarity"]
                      for og in gs])
        contradicts = sorted({o["statement"] for o in opposites})
        raw_opposites = [og for pol, gs in raw_contra.get(g["contra_key"], {}).items()
                         if pol != g["polarity"] for og in gs]
        raw_contradicts = sorted({o["statement"] for o in raw_opposites})
        row = {
            "statement": rep,
            # a polarity contradiction is the strongest contested signal -> mixed even if this side's own
            # evidence is one-directional (that is exactly what the structured key makes reachable).
            "epistemic": ("mixed" if contradicts and sup
                           else _source_guarded_epistemic(sup, opp, claim_source)),
            "maturity": item["maturity"],
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
            "verification": sorted(g["verification"]),
            "claim_uid": g["uid"], "scope": g["scope"], "polarity": g["polarity"],
            "metric": g["metric"],
            "decision": item["decision"], "contradicts": contradicts,
            "research_source": research_source,
            "claim_source": claim_source,
        }
        digest_row = {**row,
                      "epistemic": ("mixed" if raw_contradicts and sup
                                     else _source_guarded_epistemic(sup, opp, claim_source)),
                      "contradicts": raw_contradicts}
        row["evidence_digest"] = claim_evidence_digest(digest_row)
        decision_digest = str((item["decision"] or {}).get("evidence_digest") or "")
        row["decision_fresh"] = (decision_digest == row["evidence_digest"] if decision_digest else None)
        out.append(row)
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"],
                            0 if c["contradicts"] else 1, c["statement"]))
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None, fuzzy: bool = False,
                      structured: bool = False, bounded: bool = True) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Groups by normalized statement; each claim carries `support`/`oppose` node-id evidence,
    contributing `runs`/`scopes`, and an `epistemic` state. `decisions` (from `load_claim_decisions`)
    overlays an operator `maturity` (`operator-ratified`/`operator-rejected`/`operator-pinned`, else
    `machine-proposed`) — the §22.4 governance overlay. Sorted most-evidenced first. Pure.

    `structured` (opt-in, the full CR of the lean `fuzzy` merge) switches identity to the SCOPE+POLARITY-safe
    structured claim key (`claim_key.claim_signature`): claims from different tasks never merge, opposite
    polarity ("X helps" vs "X never helps") is a CONTRADICTION not a merge, and paraphrase/inflection
    variants collapse by exact structured key (O(n), no transitive over-merge). Mutually exclusive with the
    lean `fuzzy` path (structured wins)."""
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = _research_source_summary(research_claims)
    claim_source = _claim_source_summary(
        lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    if structured:
        rows = _structured_assessments(
            lessons, research_claims, decisions,
            research_source=research_source, claim_source=claim_source)
        projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
        return _ClaimAssessmentRows(
            projected, claim_source=claim_source, research_source=research_source)
    groups: dict[str, dict] = {}

    def _group(stmt: str) -> Optional[dict]:
        s = _claim_text(stmt)
        if not s:
            return None
        # NOTE (CODEX): identity here is the normalized STATEMENT (the shipped lesson `normalize_statement`
        # key) — it can merge same-worded claims across incompatible scopes and the 160-char cap can
        # collide. A structured semantic claim key (subject/intervention/comparator/scope) is the CR1b TODO
        # (§21.20.13); this lean projection keeps scope/runs as metadata on the claim.
        return groups.setdefault(normalize_statement(s), {
            "statement": s, "support": set(), "oppose": set(),
            "unverified": set(), "runs": set(), "scopes": set(), "sources": set(),
            "verification": set()})

    for lz in lessons or []:
        g = _group(lz.get("statement"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _group(rc.get("statement"))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            g["unverified"].update(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for key, g in groups.items():
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        overlay = decisions
        real_scopes = {str(scope) for scope in g["scopes"] if str(scope)}
        # A statement row spanning multiple tasks cannot safely receive any one task's policy.  For a
        # task-bound row, however, the exact scope-only decision outranks the portfolio-wide fallback.
        d = None
        if len(real_scopes) == 1:
            from looplab.engine.claim_key import claim_uid
            scope = next(iter(real_scopes))
            d = overlay.get(claim_uid(g["statement"], scope=scope, metric=""))
            # Compatibility for a custom lean overlay keyed by normalized statement+scope.
            if d is None:
                d = overlay.get(_scoped_key(key, scope))
        if d is None:
            d = overlay.get(key)
        # The lean projection groups by statement across tasks. A caller-supplied scoped decision may
        # therefore govern this row only when all contributing task scopes are that exact scope; unscoped
        # decisions remain the portfolio-wide fallback. The durable loader normally indexes scoped records
        # by structured UID only, but this guard also keeps custom/preloaded overlays fail-closed.
        if not isinstance(d, dict):
            d = None
        if d is not None:
            _dscope = str(d.get("scope") or "")
            if _dscope:
                if not real_scopes or not real_scopes <= {_dscope}:
                    d = None
        if d is None:
            d = overlay.get(_global_key(key))
        if not isinstance(d, dict):
            d = None
        if d is not None:
            d = sanitize_cross_run_projection(
                d, max_chars=16_000, max_items=64, max_total_items=256)
        out.append({
            "statement": g["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp,
            "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]),
            "sources": sorted(g["sources"]), "verification": sorted(g["verification"]),
            "decision": d,
            "research_source": research_source,
            "claim_source": claim_source,
        })
    # most-evidenced first (support+oppose), contested claims break ties toward visibility, then statement
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    rows = _fuzzy_merge_claims(out) if fuzzy else out
    projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
    return _ClaimAssessmentRows(
        projected, claim_source=claim_source, research_source=research_source)


# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def _claim_research_source_summary(claims) -> Optional[dict]:
    """Return one coherent aggregate receipt carried by all rows in an assessment snapshot."""
    carried = _safe_research_source_summary(getattr(claims, "research_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_research_source_summary(row.get("research_source")) for row in rows
                if "research_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    # A mixed/malformed snapshot is lower-bound evidence. Keep known counts for diagnosis, but fail the
    # completeness gate so no pack or steward can infer an exact positive from incompatible rows.
    base = first or _research_source_summary([])
    unknown = max(1, base["producer_unknown_runs"])
    runs = max(base["producer_runs"], unknown + base["producer_partial_runs"])
    return {
        **base,
        "source_complete": False,
        "producer_receipt_known": False,
        "producer_complete": False,
        "producer_runs": runs,
        "producer_unknown_runs": unknown,
    }


def _claim_claim_source_summary(claims) -> Optional[dict]:
    """Return one coherent lessons+research authority receipt, including for an empty snapshot."""
    carried = _safe_claim_source_summary(getattr(claims, "claim_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_claim_source_summary(row.get("claim_source")) for row in rows
                if "claim_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    return _unknown_claim_source_summary()


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5,
                       _concept_rows: Optional[list[dict]] = None,
                       _research_source: Optional[dict] = None,
                       _claim_source: Optional[dict] = None) -> dict:
    """Assemble a CLAIM-COUNT-bounded cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). ("Claim-count", not token/byte: the pack caps the
    number of claims + per-claim field lengths; a true serialized-token envelope is the CR2b TODO — see the
    NOTE below.) The design's hard rule is that positive hits must
    never crowd out caveats. Precedence is pinned → ratified → mixed → supported → refuted →
    inconclusive, and a **caveat slot is reserved** whenever it can be filled by replacing the weakest
    non-pinned positive. The hard claim cap is never exceeded; pins beyond it are reported as omitted.
    Pure/deterministic and
    'silent' by construction — it just returns structured data; promoting it to advisory prompt-grounding
    is a separate, gated step (never wired here). No LLM, no I/O."""
    # NOTE (CODEX): this bounds by CLAIM COUNT + per-claim field caps (below), not a serialized token/byte
    # budget — a true token envelope is the CR2b TODO. `max_claims<1` is normalized to 1.
    max_claims = max(1, min(int(max_claims), _MAX_CONTEXT_CLAIMS))
    # Governance precedence is explicit: rejected is absent; pinned is retention-critical; ratified is the
    # next preference; then evidence ordering. A caveat may replace a non-pinned positive, never a pin.
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    _kept = {"operator-pinned", "operator-ratified"}
    pinned = [c for c in live if c.get("maturity") == "operator-pinned"]
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    rest = [c for c in live if c.get("maturity") not in _kept]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    ordered = (pinned + ratified + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest NON-kept
    # picked (a governance-retained claim is never evicted to make room) for the strongest available caveat —
    # opposition is never crowded out by a full slate of positives (§20.5). Kept caveats count as caveats too.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        # Include RATIFIED caveats too: a ratified mixed/refuted/inconclusive claim pushed past max_claims by
        # the ratified block must still be able to fill the reserved slot, or a slate of ratified-supported
        # claims could crowd opposition out — the exact §20.5 rule this slot exists to protect (CODEX).
        caveats = ([c for c in pinned if c["epistemic"] in _CAVEAT_STATES]
                   + [c for c in ratified if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        # Evict the weakest non-pinned positive. Ratification raises priority but may still yield to a
        # caveat; a pin is the explicit retention guarantee and cannot be displaced. If the cutoff is all
        # pins there is no legal victim, so the caveat remains outside this bounded projection.
        victim = next((i for i in range(len(picked) - 1, -1, -1)
                       if picked[i].get("maturity") != "operator-pinned"), None)
        if caveats and victim is not None:
            picked = picked[:victim] + picked[victim + 1:] + [caveats[0]]

    def _slim(c: dict) -> dict:
        # Evidence refs are run-QUALIFIED ("run:node"), so the truncated support/oppose lists stay citable;
        # keep runs/scopes too so a reader can resolve the claim's provenance (CODEX).
        return {"statement": _claim_text(c.get("statement"), 300), "epistemic": c["epistemic"],
                "maturity": c.get("maturity", "machine-proposed"),
                "claim_uid": c.get("claim_uid", ""), "scope": c.get("scope", ""),
                "evidence_digest": c.get("evidence_digest", ""),
                "decision_fresh": c.get("decision_fresh"),
                "metric": c.get("metric", ""), "polarity": c.get("polarity"),
                "n_support": c["n_support"], "n_oppose": c["n_oppose"],
                "n_unverified": c.get("n_unverified", 0),
                "support": c["support"][:6], "oppose": c["oppose"][:6],
                "unverified": c.get("unverified", [])[:6],
                # Structured polarity contradictions are assertion-level counter-evidence,
                # not entries in ``oppose``. Keep their bounded text or a mixed claim renders as 1↑/0↓
                # with no visible reason for the disagreement.
                "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
                "runs": [_identity_text(value, 500) for value in c.get("runs", [])[:6]],
                "scopes": [_identity_text(value, _MAX_DECISION_SCOPE)
                           for value in c.get("scopes", [])[:6]]}

    pack = {
        "claims": [_slim(c) for c in picked],
        "n_claims_total": len(claims or []),
        "n_contested": sum(1 for c in live if c.get("epistemic") == "mixed"),
        # Pins have highest priority but cannot override the hard prompt-size cap. Surface any overflow
        # explicitly so a bounded advisory never implies that it retained every operator pin.
        "n_pinned_total": len(pinned),
        "n_pinned_omitted": max(0, len(pinned) - sum(
            1 for c in picked if c.get("maturity") == "operator-pinned")),
    }
    research_source = (_safe_research_source_summary(_research_source)
                       if _research_source is not None
                       else _claim_research_source_summary(claims))
    if _research_source is not None and research_source is None:
        research_source = {
            **_research_source_summary([]),
            "source_complete": False,
            "producer_receipt_known": False,
            "producer_complete": False,
            "producer_runs": 1,
            "producer_unknown_runs": 1,
        }
    if research_source is not None:
        pack["research_source"] = research_source
    claim_source = (_safe_claim_source_summary(_claim_source)
                    if _claim_source is not None else _claim_claim_source_summary(claims))
    if _claim_source is not None and claim_source is None:
        claim_source = _unknown_claim_source_summary()
    if claim_source is not None:
        pack["claim_source"] = claim_source
    if concept_overview:
        from looplab.engine.memory import concept_profit_tendencies
        # CODEX AGENT: callers that own the retained capsule snapshot may supply its private pre-cap rows.
        # The pack still emits only `max_claims` labels/tendencies; this prevents the public overview's
        # display cap from becoming a silent analytics cap while keeping the outward prompt bounded.
        row_source = (_concept_rows if _concept_rows is not None
                      else concept_overview.get("concepts"))
        rows = [e for e in (row_source or []) if isinstance(e, dict)]
        source_complete = concept_overview.get("source_complete") is True
        # PART V Phase 1 profit signal: surface concepts with a CONSISTENT, MULTI-RUN rank tendency (advisory
        # only — prompts, never selection). The threshold lives in ONE shared helper so the context pack and
        # the cross_run_atlas tool can never diverge; a concept with mixed/thin evidence appears in neither.
        # CODEX AGENT: consistency also needs a complete denominator. A non-matching partial capsule may
        # have omitted this exact concept and an opposite sign, so retained positive rows remain observable
        # below but cannot support a directional portfolio tendency until every capsule receipt is exact.
        tendency = (concept_profit_tendencies(rows, limit=max_claims) if source_complete
                    else {"helps": [], "hurts": []})
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            # A hand-built/older overview with no receipt is UNKNOWN, never silently exact.
            "source_complete": source_complete,
            "partial_capsules": concept_overview.get(
                "partial_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_unknown_capsules": concept_overview.get(
                "source_unknown_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_concepts_omitted": concept_overview.get("source_concepts_omitted", 0),
            "source_outcomes_omitted": concept_overview.get("source_outcomes_omitted", 0),
            "source_store_complete": concept_overview.get(
                "source_store_complete", source_complete) is True,
            "source_rows_total": concept_overview.get("source_rows_total", 0),
            "source_rows_quarantined": concept_overview.get("source_rows_quarantined", 0),
            "source_malformed_rows": concept_overview.get("source_malformed_rows", 0),
            "source_invalid_capsule_rows": concept_overview.get(
                "source_invalid_capsule_rows", 0),
            "source_duplicate_run_rows": concept_overview.get("source_duplicate_run_rows", 0),
            "top_concepts": [_claim_text(e.get("concept"), 500) for e in rows[:max_claims]],
            # E3: keep the run COUNT (n_helped/n_hurt) in the rendered span — "loss/contrastive (n=7)"
            # vs "(n=2)" tells the Researcher how strong the multi-run tendency is, not just its direction.
            "helps": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["helps"]],
            "hurts": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["hurts"]],
        }
    return pack


# Deterministic query-INTENT cues (CR2a eligibility). Kept ML-context-safe: ambiguous technique words
# ("negative", "loss") are NOT cues, so "hard negatives for retrieval" reads as neutral EXPLORE, not FAILED.
_INTENT_CUES = {
    "failed":    frozenset("fail failed failing avoid avoided pitfall pitfalls mistake mistakes wrong "
                           "broke broken regress regression hurt hurts degrade degrades harmful useless "
                           "ineffective".split()),
    "contested": frozenset("contested contradict contradiction conflict conflicting disagree disagreement "
                           "controversial controversy debate unclear uncertain".split()),
    "worked":    frozenset("best proven effective recommend recommended success successful reliable robust "
                           "winning champion".split()),
}
# The CONTRADICTION pool for the retrieval quota — claims that carry actual OPPOSITION (mixed=contested,
# refuted=negative verdict). This is DELIBERATELY narrower than build_context_pack's `_CAVEAT_STATES`
# (which also includes `inconclusive`): the context-pack reserves a slot so a clean slate of positives can't
# hide any NON-positive (§21.20.5 coverage), whereas the retrieval quota reserves slots specifically for
# COUNTER-EVIDENCE/contradictions — an inconclusive (no-stance) claim is neither. Two distinct mechanisms,
# not an accidental inconsistency (concept-conformance).
_CAVEAT = frozenset(("mixed", "refuted"))


def _classify_intent(query: str) -> str:
    """Map a free-text query to a retrieval INTENT (failed / contested / worked / explore) by cue overlap.
    Deterministic, no LLM. `explore` (neutral) when no cue fires — the safe default that reorders nothing."""
    toks = set(_CLAIM_WORD.findall(str(query or "").casefold()))
    scored = [(sum(1 for w in cues if w in toks), name) for name, cues in _INTENT_CUES.items()]
    best_n, best = max(scored, key=lambda t: (t[0], t[1]))
    return best if best_n else "explore"


def _eligible(kind: str, meta: dict, intent: str) -> bool:
    """Whether a doc is on-INTENT (a soft priority signal, never a hard exclusion — counter-evidence is
    still returned). Concepts are always eligible; a claim's eligibility depends on its epistemic/maturity."""
    if kind != "claim" or intent == "explore":
        return True
    ep, mat = meta.get("epistemic"), meta.get("maturity")
    if intent == "failed":
        return ep in _CAVEAT
    if intent == "contested":
        return ep == "mixed"
    if intent == "worked":
        return ep == "supported" or mat == "operator-ratified"
    return True


_INTENTS = ("failed", "contested", "worked", "explore")

# Document ids are a stable identity for the same searchable statement/concept.  The corpus digest has a
# separate schema because it also commits to aggregate source receipts that do not belong in every doc id.
_RETRIEVAL_DOCUMENT_VERSION = 2
_RETRIEVAL_CORPUS_VERSION = 7
_INTENT_SCORE_BONUS = 0.001
_CAVEAT_SCORE_RATIO = 0.50
_CAVEAT_QUERY_COVERAGE = 0.10


def _retrieval_tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return frozenset(_CLAIM_WORD.findall(normalized))


def _lexical_relevance(query: str, text: str) -> tuple[int, float, float]:
    q, d = _retrieval_tokens(query), _retrieval_tokens(text)
    shared = len(q & d)
    coverage = shared / len(q) if q else 0.0
    jaccard = shared / len(q | d) if q or d else 0.0
    return shared, coverage, jaccard


def _json_digest(value, *, length: int = 20) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _retrieval_doc(kind: str, text: str, meta: dict) -> tuple[str, str, dict]:
    identity = {"v": _RETRIEVAL_DOCUMENT_VERSION, "kind": kind,
                "claim_uid": str(meta.get("claim_uid") or ""),
                "metric": str(meta.get("metric") or ""),
                "text": " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())}
    stable_id = f"{kind[:1]}_{_json_digest(identity, length=16)}"
    return kind, str(text or ""), {**meta, "stable_id": stable_id}


def _retrieval_corpus_digest(docs, *, concept_source: dict, research_source: dict,
                             claim_source: dict) -> str:
    canonical = [{"kind": kind, "text": text, "meta": meta}
                 for kind, text, meta in sorted(docs, key=lambda d: d[2]["stable_id"])]
    envelope = {"v": _RETRIEVAL_CORPUS_VERSION, "docs": canonical,
                "concept_source": concept_source, "research_source": research_source,
                "claim_source": claim_source}
    return _json_digest(envelope, length=20)


def _preselect_retrieval_docs(docs, query: str, limit: int):
    """Cheap query-aware cap with one best row per source kind before the expensive hybrid index."""
    cap = max(1, int(limit))
    if len(docs) <= cap:
        return list(docs)
    stats = [_lexical_relevance(query, d[1]) for d in docs]
    ranked = sorted(range(len(docs)),
                    key=lambda i: (-stats[i][0], -stats[i][1], -stats[i][2],
                                   docs[i][2]["stable_id"]))
    selected: list[int] = []
    kinds = sorted({d[0] for d in docs})
    if cap >= len(kinds):
        for kind in kinds:
            selected.append(next(i for i in ranked if docs[i][0] == kind))
    selected_set = set(selected)
    selected.extend(i for i in ranked if i not in selected_set)
    return [docs[i] for i in selected[:cap]]


def cross_run_retrieve(memory_dir, query: str, *, k: int = 8, lessons=None, capsules=None,
                       research_claims=None, scope_task: str = "", contradiction_quota: float = 0.34,
                       max_corpus: int = 2000, structured: bool = False, intent: Optional[str] = None,
                       scope_receipt: Optional[dict] = None) -> dict:
    """CR2a retrieval planner (§21.20.5, full CR): RRF-fuse the portfolio's cross-run KNOWLEDGE — claims
    (epistemic state / operator maturity) + concepts (#runs) — over the shipped `HybridRetriever`
    (lexical + BM25 + vector; reuses hybrid_merge, NO new fuser), then shape the ranked recall with:

    - INTENT classification (`failed`/`contested`/`worked`/`explore`) → an eligibility priority so an
      on-intent claim floats up (soft; never hides counter-evidence);
    - a CONTRADICTION QUOTA reserving ~`contradiction_quota` of the k slots for caveat (mixed/refuted)
      claims when they exist, so a positive-heavy recall never buries the counter-evidence (mirrors the
      context pack's caveat slot). `failed`/`contested` intents raise the quota;
    - a bounded corpus (`max_corpus`, truncation REPORTED not silent) + a why-recalled RECEIPT (intent,
      quota, corpus digest, degraded-channel note, per-hit rank).

    Every source is SCOPED before indexing: pass scoped `lessons`/`capsules` plus their aggregate
    `scope_receipt`, and `scope_task` filters the D8 research claims to that task so a task-bound agent
    cannot retrieve another task's claims (CODEX).
    Operator-rejected claims never enter the corpus. Advisory; pure w.r.t. the passed/loaded stores."""
    from pathlib import Path

    from looplab.engine.concept_registry import (load_concept_aliases, load_concept_splits)
    from looplab.engine.memory import (ConceptCapsuleStore, _filter_capsule_rows,
                                       _portfolio_concept_overview_data)
    base = Path(memory_dir) if memory_dir else None
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    # Scope EVERY source before joining. Decisions are a governance overlay; they never grant visibility.
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    if scope_task:
        wanted = str(scope_task)
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        capsules = _filter_capsule_rows(
            capsules, lambda r: str(r.get("task_id") or "") == wanted)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    research = _valid_claim_source_rows(research, research=True)
    research_source = _research_source_summary(research)
    claims = _filter_claim_assessments(
        claim_assessments(lessons, research_claims=research,
                          decisions=load_claim_decisions(memory_dir), structured=structured),
        lambda c: c.get("maturity") != "operator-rejected")
    claim_source = (_safe_claim_source_summary(claims.claim_source)
                    or _claim_source_summary(lessons, research, research_source=research_source))
    overview, concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=load_concept_aliases(memory_dir),
        splits=load_concept_splits(memory_dir))
    # CODEX AGENT: source completeness is part of the retrieval corpus, even when a query happens to match
    # only claims or the same retained concept rows.  Aggregate it across every eligible capsule before
    # query preselection so legacy/omitted concepts cannot masquerade as authoritative absence or exact
    # frequency, and a partial->complete transition changes the auditable corpus identity.
    concept_source = {
        "n_capsules": overview["n_runs"],
        "source_complete": overview.get("source_complete") is True,
        "partial_capsules": int(overview.get("partial_capsules", 0) or 0),
        "source_unknown_capsules": int(overview.get("source_unknown_capsules", 0) or 0),
        "source_concepts_omitted": int(overview.get("source_concepts_omitted", 0) or 0),
        "source_outcomes_omitted": int(overview.get("source_outcomes_omitted", 0) or 0),
        # The public overview is independently bounded. Commit both its display omission and the exact
        # retained concept cardinality to the corpus identity so a cap change/tail cannot look identical.
        "concepts_total": len(concept_rows),
        "overview_concepts_omitted": int(overview.get("concepts_omitted", 0) or 0),
        "source_store_complete": overview.get("source_store_complete") is True,
        "source_rows_total": int(overview.get("source_rows_total", 0) or 0),
        "source_rows_quarantined": int(overview.get("source_rows_quarantined", 0) or 0),
        "source_malformed_rows": int(overview.get("source_malformed_rows", 0) or 0),
        "source_invalid_capsule_rows": int(
            overview.get("source_invalid_capsule_rows", 0) or 0),
        "source_duplicate_run_rows": int(overview.get("source_duplicate_run_rows", 0) or 0),
    }
    scope_keys = (
        "scope_unknown_capsules", "scope_fingerprint_unknown_capsules",
        "scope_fingerprint_items_omitted", "scope_direction_unknown_capsules",
    )
    if scope_receipt is None:
        scope_source = {"scope_receipt_known": True, "scope_complete": True,
                        **{key: 0 for key in scope_keys}}
    else:
        source = scope_receipt if isinstance(scope_receipt, dict) else {}
        counts_valid = all(
            isinstance(source.get(key), int) and not isinstance(source.get(key), bool)
            and source.get(key) >= 0 for key in scope_keys
        )
        complete_valid = type(source.get("scope_complete")) is bool
        unknown = source.get("scope_unknown_capsules") if counts_valid else 0
        fingerprint_unknown = source.get("scope_fingerprint_unknown_capsules", 0) if counts_valid else 0
        direction_unknown = source.get("scope_direction_unknown_capsules", 0) if counts_valid else 0
        consistent = (complete_valid and counts_valid
                      and source.get("scope_complete") == (unknown == 0)
                      and fingerprint_unknown + direction_unknown <= unknown)
        scope_source = {
            "scope_receipt_known": consistent,
            # CODEX AGENT: a caller-supplied malformed applicability receipt fails closed. Retrieval may
            # retain its positive documents, but neither an empty result nor a frequency is exact.
            "scope_complete": consistent and source.get("scope_complete") is True,
            **{key: source.get(key) if counts_valid else 0 for key in scope_keys},
        }
    concept_source.update(scope_source)
    docs: list[tuple[str, str, dict]] = []
    for c in claims:
        evidence_digest = _json_digest({"support": c.get("support", []), "oppose": c.get("oppose", []),
                                        "unverified": c.get("unverified", []),
                                        "sources": c.get("sources", []),
                                        "research_source": c.get("research_source"),
                                        "claim_source": c.get("claim_source")})
        docs.append(_retrieval_doc("claim", c["statement"], {
            "epistemic": c["epistemic"], "n_support": c["n_support"],
            "n_oppose": c["n_oppose"], "n_unverified": c.get("n_unverified", 0),
            "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
            "maturity": c.get("maturity"), "claim_uid": c.get("claim_uid", ""),
            "metric": c.get("metric", ""), "scopes": c.get("scopes", []),
            "research_source": c.get("research_source", research_source),
            "claim_source": c.get("claim_source", claim_source),
            "decision_revision": (c.get("decision") or {}).get("revision"),
            "governance_digest": _json_digest(c.get("decision") or {}),
            "evidence_digest": evidence_digest}))
    # CODEX AGENT: query-aware preselection must see every validated canonical row. Iterating the public
    # top-512 projection made concept #513 look absent with source_complete=true and truncated=0.
    for e in concept_rows:
        docs.append(_retrieval_doc("concept", _claim_text(e.get("concept"), 500), {
            "n_runs": e["n_runs"],
            "runs": [_identity_text(r.get("run_id"), 500) for r in e["runs"][:5]
                     if isinstance(r, dict)],
            "evidence_digest": _json_digest(e["runs"])}))

    n_total = len(docs)
    max_corpus = max(1, min(int(max_corpus), _MAX_RETRIEVAL_CORPUS))
    indexed_docs = _preselect_retrieval_docs(docs, str(query or ""), max_corpus)
    truncated = n_total - len(indexed_docs)
    concepts_indexed = sum(kind == "concept" for kind, _text, _meta in indexed_docs)
    claims_indexed = sum(kind == "claim" for kind, _text, _meta in indexed_docs)
    projection_receipt = {
        "concepts_indexed": concepts_indexed,
        "concepts_omitted": len(concept_rows) - concepts_indexed,
        "claims_total": len(claims),
        "claims_indexed": claims_indexed,
        "claims_omitted": len(claims) - claims_indexed,
    }
    corpus_digest = _retrieval_corpus_digest(
        docs, concept_source=concept_source, research_source=research_source,
        claim_source=claim_source)
    indexed_source = {**concept_source, **projection_receipt}
    retrieval_source = {**indexed_source, "research_source": research_source,
                        "claim_source": claim_source}
    # The AGENT may pass an explicit `intent` (it knows why it is searching — genuinely agentic); otherwise
    # classify deterministically from the query text. An unknown value falls back to classification.
    intent = intent if intent in _INTENTS else _classify_intent(query)
    kk = max(1, min(int(k), _MAX_RETRIEVAL_HITS))
    try:
        base_quota = float(contradiction_quota)
    except (TypeError, ValueError):
        base_quota = 0.34
    if not math.isfinite(base_quota):
        base_quota = 0.34
    base_quota = min(1.0, max(0.0, base_quota))
    q = max(base_quota, 0.5) if intent in ("failed", "contested") else base_quota
    target = min(math.ceil(kk * q), max(0, kk - 1))
    # A why-recalled receipt: corpus revision (content digest), the degraded vector-channel semantics, the
    # classified intent + quota, and (below) the per-hit rank — enough to explain/reproduce a result.
    receipt = {"query": _claim_text(query, 4000), "k": kk, "n_corpus": n_total,
               "n_indexed": len(indexed_docs), "corpus_digest_version": _RETRIEVAL_CORPUS_VERSION,
               "channels": ["lexical", "bm25", "vector"], "intent": intent,
               "vector_channel": "hash_embed(64-bucket bag-of-words; lexical proxy, not semantic)",
               "corpus_digest": corpus_digest,
               "retrieval_digest": _retrieval_corpus_digest(
                   indexed_docs, concept_source=indexed_source,
                   research_source=research_source, claim_source=claim_source),
               "truncated": truncated,
               "preselection": "query-overlap+one-per-source/v1",
               "contradiction_quota": round(base_quota, 3),
               "effective_quota": round(q, 3), "caveat_target": target,
               "caveat_score_ratio": _CAVEAT_SCORE_RATIO,
               "caveat_query_coverage": _CAVEAT_QUERY_COVERAGE,
               "intent_score_bonus": _INTENT_SCORE_BONUS,
               **retrieval_source}
    if not indexed_docs or not str(query or "").strip():
        return {"results": [], "receipt": {**receipt, "n_hits": 0, "n_caveats": 0}}

    from looplab.search.hybrid_merge import HybridRetriever
    # Retrieve a POOL larger than k so the intent priority + contradiction quota have room to reorder/swap
    # without extra queries; the vector channel is the `hash_embed` bag-of-words (a lexical proxy — declared
    # in the receipt, not passed off as semantic retrieval).
    pool_n = min(len(indexed_docs), max(kk * 4, kk + 12))
    pool = HybridRetriever([t for _, t, _ in indexed_docs]).candidates(str(query), k=pool_n)
    ranked = []
    for rel_rank, (i, score) in enumerate(pool):
        kind, text, meta = indexed_docs[i]
        shared, coverage, jaccard = _lexical_relevance(str(query), text)
        eligible = _eligible(kind, meta, intent)
        # Intent is a bounded tiebreak-like bonus scaled by actual query overlap, never a hard tier that can
        # lift an unrelated "failed" memory above a strongly relevant positive result.
        bonus = (_INTENT_SCORE_BONUS * min(1.0, coverage * 2.0)
                 if intent != "explore" and eligible and shared else 0.0)
        ranked.append({"idx": i, "kind": kind, "text": text, "score": round(float(score), 6),
                       "intent_bonus": round(bonus, 6), "query_overlap": shared,
                       "query_coverage": round(coverage, 4), "query_jaccard": round(jaccard, 4),
                       "rel_rank": rel_rank, **meta})
    ranked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]), h["rel_rank"], h["stable_id"]))
    picked = ranked[:kk]

    # CONTRADICTION QUOTA: guarantee ~quota of the k slots are caveat (mixed/refuted) claims when the pool
    # has them — swapping the LEAST-relevant non-caveat picks (from the bottom) for the most-relevant unpicked
    # caveats, so the top relevance hit is never displaced and opposition is never crowded out.
    # ceil(k*q) caveat slots, but capped at k-1 so the #1 relevance hit is NEVER evicted (at k=1 the target
    # is 0 — the single slot stays the top hit, as the swap contract promises; mega-review finding).
    have = [h for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
    if target > len(have):
        picked_ids = {h["idx"] for h in picked}
        top_score = max((h["score"] for h in ranked), default=0.0)
        extra = [h for h in ranked if h["idx"] not in picked_ids
                 and h["kind"] == "claim" and h.get("epistemic") in _CAVEAT
                 and h["query_coverage"] >= _CAVEAT_QUERY_COVERAGE
                 and h["score"] >= top_score * _CAVEAT_SCORE_RATIO]
        need = target - len(have)
        for cav in extra[:need]:
            # Keep the raw relevance winner (rel_rank 0). Quotas reserve relevant counter-evidence, not an
            # unrelated caveat selected solely for its epistemic label. Also NEVER evict an operator-PINNED
            # claim — the "pinned is retained" governance projection applies to EVERY consumer, not just the
            # context pack (concept-conformance: §22.4 / §21.20.5, mirroring build_context_pack).
            victim = next((h for h in reversed(picked)
                           if not (h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
                           and h["rel_rank"] != 0 and h.get("maturity") != "operator-pinned"), None)
            if victim is None:
                break
            picked[picked.index(victim)] = cav
        picked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]),
                                   h["rel_rank"], h["stable_id"]))

    n_caveats = sum(1 for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
    results = [{k2: v for k2, v in h.items() if k2 != "idx"} for h in picked]
    # Report the EFFECTIVE quota actually applied (raised for failed/contested) + the reserved caveat target,
    # so the receipt explains why a contested claim was (or wasn't) surfaced — not just the configured base.
    return {"results": results,
            "receipt": {**receipt, "n_hits": len(results), "n_caveats": n_caveats}}


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None, research_claims: Optional[list[dict]] = None,
                    aliases: Optional[dict] = None, splits: Optional[dict] = None,
                    structured: bool = False) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured bounded observation/mixed-evidence
    view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    The legacy ``thin_coverage`` field means only "observed in one returned run". It is not a gap or coverage
    assertion: a true CoverageFrame (§20.6, unknown-vs-zero) needs a frozen scope, eligible denominator and
    health contract, which remain deferred full-CR3a work."""
    from looplab.engine.memory import _dedup_valid_capsules, _portfolio_concept_overview_data
    max_items = max(1, min(int(max_items), 100))             # route/CLI-independent hard envelope
    source_capsules = capsules if isinstance(capsules, (list, tuple)) else []
    capsules = _dedup_valid_capsules(source_capsules)
    overview, full_concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=aliases, splits=splits)
    # Keep the complete internal sets for exact run totals and the governance evidence digest. Only the
    # outward contradictions/context projections are capped below.
    claims = claim_assessments(lessons, research_claims=research_claims, decisions=decisions,
                               structured=structured, bounded=False)
    research_source = (_safe_research_source_summary(getattr(claims, "research_source", None))
                       or _research_source_summary(
                           _valid_claim_source_rows(research_claims, research=True)))
    claim_source = (_safe_claim_source_summary(getattr(claims, "claim_source", None))
                    or _claim_source_summary(lessons, research_claims,
                                             research_source=research_source))
    # A contradiction the operator REJECTED is no longer live, consistent with build_context_pack and
    # cross_run_claims. Pin priority applies inside the embedded context pack; this human-facing contested
    # summary remains evidence-ordered and independently capped.
    contested = [c for c in claims if c["epistemic"] == "mixed" and c.get("maturity") != "operator-rejected"]
    # CODEX AGENT: Atlas is independently bounded. Derive single-run observations and rank tendencies from
    # every canonical retained row BEFORE its outward cap; the old overview-capped path silently returned
    # `thin_coverage=[]` once 512 more-frequent concepts occupied the entire overview projection.
    thin = [e["concept"] for e in full_concept_rows if e["n_runs"] == 1]
    # Run count spans BOTH sources — capsules AND the runs cited by lessons — so a lesson-only / legacy
    # memory (no opt-in capsules) is not reported as zero runs (CODEX). The authoritative scoped corpus
    # join (cross_run_index) is the full-CR TODO; this at least unions what the two memory stores know.
    run_ids = {c.get("run_id") for c in capsules if c.get("run_id")}
    for cl in claims:
        run_ids.update(cl.get("runs") or [])
    n_runs = len(run_ids)
    # Keep the embedded context-pack coverage n_runs CONSISTENT with the top-level count (both the union of
    # capsule + lesson-cited runs), so one atlas payload never reports two different run counts — otherwise a
    # lesson-only memory says n_runs>0 at the top but coverage.n_runs==0, the very "zero runs" artifact the
    # union set out to fix (CODEX).
    pack_overview = {**overview, "n_runs": n_runs}
    explored = full_concept_rows[:max_items]
    thin_coverage = thin[:max_items]
    contradictions = [_bounded_claim_projection(row) for row in contested[:max_items]]
    payload = {
        "n_runs": n_runs, "n_concepts": overview["n_concepts"],
        "n_claims": len(claims), "n_contested": len(contested),
        # CODEX AGENT: the Atlas UI must not infer capsule-source completeness from returned rows or from
        # transport freshness. Keep one small aggregate receipt at the read-model boundary; the embedded
        # context-pack copy remains for agents and backward-compatible consumers.
        "concept_source": {key: overview[key] for key in (
            "source_complete", "partial_capsules", "source_unknown_capsules",
            "source_concepts_omitted", "source_outcomes_omitted",
            "source_store_complete", "source_rows_total", "source_rows_quarantined",
            "source_malformed_rows", "source_invalid_capsule_rows",
            "source_duplicate_run_rows",
        )},
        "research_source": research_source,
        "claim_source": claim_source,
        "explored": explored,                               # what's been tried (concept × runs)
        "explored_total": len(full_concept_rows),
        "explored_omitted": len(full_concept_rows) - len(explored),
        "thin_coverage": thin_coverage,                     # legacy key: observed in one returned run
        "thin_coverage_total": len(thin),
        "thin_coverage_omitted": len(thin) - len(thin_coverage),
        "contradictions": contradictions,
        "contradictions_total": len(contested),
        "contradictions_omitted": len(contested) - len(contradictions),
        "context_pack": build_context_pack(
            claims, concept_overview=pack_overview, max_claims=max_items,
            _concept_rows=full_concept_rows, _research_source=research_source,
            _claim_source=claim_source),
    }
    return sanitize_cross_run_projection(
        payload, max_chars=128_000_000, max_items=128, max_total_items=100_000)


def _safe_text(s, limit: int = 120) -> str:
    """Sanitize UNTRUSTED memory text (claim statements / concept slugs — LLM/repo-derived) before it enters
    an agent prompt: strip control chars + collapse newlines/whitespace to a single space, then bound the
    length. Prevents newline/control-char prompt-injection through the cross-run advisory pack (mega-review)."""
    return _claim_text(s, limit)


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; retains mixed evidence so the agent sees counter-arguments, not only positives.
    All memory-derived text is sanitized (control chars/newlines stripped) — quoted DATA, not instructions
    (mega-review prompt-injection hardening)."""
    if (not pack.get("claims") and not pack.get("coverage")
            and not pack.get("research_source") and not pack.get("claim_source")):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claim records, "
             f"{pack.get('n_contested', 0)} mixed-evidence) — bounded observations, with counter-evidence:"]
    if pack.get("n_pinned_omitted", 0):
        lines.append(
            f"  WARNING: {int(pack['n_pinned_omitted'])} operator-pinned claim(s) omitted by the "
            "hard context limit; consult the full claims ledger.")
    research_source = _safe_research_source_summary(pack.get("research_source"))
    if research_source is not None and research_source["source_complete"] is not True:
        lines.append(
            "  WARNING: D8 research-claim source is PARTIAL/UNKNOWN "
            f"({research_source['producer_partial_runs']} capped run(s); "
            f"{research_source['producer_claims_omitted']} claim(s) known omitted"
            + (f"; {research_source['producer_unknown_runs']} legacy/malformed run receipt(s)"
               if research_source["producer_unknown_runs"] else "")
            + "); retained evidence is a lower bound and exact one-sided states are withheld.")
    claim_source = _safe_claim_source_summary(pack.get("claim_source"))
    if claim_source is None and "claim_source" in pack:
        lines.append(
            "  WARNING: claim evidence source receipt is malformed/unknown; exact one-sided states and "
            "absence are withheld.")
    elif claim_source is not None and claim_source["read_complete"] is not True:
        lessons_bad = claim_source["lessons"]["rows_quarantined"]
        research_bad = claim_source["research"]["rows_quarantined"]
        lines.append(
            "  WARNING: claim evidence stores are PARTIAL "
            f"(lessons quarantined={lessons_bad}; research quarantined={research_bad}); "
            "retained evidence is a lower bound and absence is not exact.")
    for c in pack.get("claims", []):
        statement = _safe_text(c.get("statement"), 120)
        contradicts = "; ".join(
            repr(_safe_text(value, 160))
            for value in (c.get("contradicts") or [])[:3])
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"UNTRUSTED_MEMORY={statement!r}"
                     + (f"; contradicts={contradicts}" if contradicts else ""))
    cov = pack.get("coverage")
    if cov:
        if cov.get("source_complete") is not True:
            lines.append(
                "  WARNING: concept capsule source is PARTIAL "
                f"({int(cov.get('partial_capsules', 0))} capsule(s); "
                f"{int(cov.get('source_concepts_omitted', 0))} concept(s) and "
                f"{int(cov.get('source_outcomes_omitted', 0))} outcome(s) known omitted"
                + (f"; {int(cov.get('source_unknown_capsules', 0))} legacy capsule(s) have unknown totals"
                   if cov.get("source_unknown_capsules", 0) else "")
                + (f"; {int(cov.get('source_rows_quarantined', 0))} durable row(s) were quarantined"
                   if cov.get("source_rows_quarantined", 0) else "")
                + "); "
                "coverage describes returned observations only; directional tendencies are withheld.")
        top = ", ".join(repr(_safe_text(x, 100))
                        for x in cov.get("top_concepts", [])[:6])
        lines.append(f"Bounded live concept observations (not coverage): {cov.get('n_runs', 0)} returned "
                     f"run(s), {cov.get('n_concepts', 0)} concept(s)"
                     f"{'; UNTRUSTED_MEMORY_CONCEPTS=' + top if top else ''}.")
        # Phase 1 profit signal: a direction-normalized RANK tendency across similar runs — which concepts
        # tended to land in the better vs worse half of their run's own field. ADVISORY — a prior rank
        # tendency, never causal proof, never a rule, and never a selection input; weigh but do not obey.
        helps = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("helps") or [])[:6])
        hurts = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("hurts") or [])[:6])
        if helps or hurts:
            # CODEX AGENT: concept slugs are persisted, LLM-originated data. Keep the explicit trust
            # marker on rank tendencies just as on the coverage line and the sibling cross-run tool;
            # repr quoting alone does not tell a proposing model that the span is inert memory.
            parts = ([f"tended to RANK BETTER UNTRUSTED_MEMORY={helps}"] if helps else []) + (
                [f"tended to RANK WORSE UNTRUSTED_MEMORY={hurts}"] if hurts else [])
            lines.append("Cross-run concept rank tendency (better/worse half of each run vs its sibling "
                         "concepts; advisory, NOT a rule — consider toward the first, scrutinize the "
                         "second): " + "; ".join(parts) + ".")
    return "\n".join(lines)
