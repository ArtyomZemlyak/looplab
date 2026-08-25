"""Bounded canonical forms for untrusted advisory sidecars.

Research memos and generated reports are audit/UI data, not replay authority, but malformed legacy
events still flow through replay and downstream cadence checks.  Normalize at both writer and replay
boundaries so an oversized or wrong-shaped sidecar cannot crash the engine or exhaust a renderer.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math

from looplab.core.jsonutil import canonical_json_digest, valid_digest_ref
from looplab.core.redact import (bounded_redacted_tree, is_secret_key_name,
                                 redact_persisted_text)
from looplab.core.source_identity import canonical_source_ref, valid_source_identity


MAX_RESEARCH_SOURCES = 64
MAX_RESEARCH_CLAIMS = 64
MAX_RESEARCH_NODE_REFS = 8
MAX_RESEARCH_URL_REFS = 4
RESEARCH_RECEIPT_VERSION = 1
_MAX_ADVISORY_TEXT = 64_000
_MAX_TREE_ITEMS = 512
_MAX_VERIFICATION_TEXT = 24_000
_MAX_VERIFICATION_VERDICTS = 64
_MAX_ADVISORY_COUNT = (1 << 63) - 1
_VERDICTS = frozenset({"supported", "unsupported", "unclear", "cited"})
_ADVISORY_REF_NAMESPACES = frozenset({"memo", "lesson", "claim"})
_ADVISORY_REF_PREFIXES = {
    namespace: f"{namespace}:sha256:" for namespace in _ADVISORY_REF_NAMESPACES
}

_CROSS_RUN_AVAILABLE_KEYS = frozenset({
    "v", "scope_task", "excluded_run", "n_lessons", "n_capsules", "n_research",
    "concept_scope", "claim_source", "corpus_digest", "render_digest",
})
_CROSS_RUN_UNAVAILABLE_KEYS = frozenset({"v", "status", "complete", "governance"})
_CROSS_RUN_CONCEPT_SCOPE_KEYS = frozenset({
    "scope_complete", "scope_unknown_capsules", "scope_fingerprint_unknown_capsules",
    "scope_fingerprint_items_omitted", "scope_direction_unknown_capsules",
})
_CROSS_RUN_CLAIM_SOURCE_KEYS = frozenset({
    "v", "receipt_known", "source_complete", "read_complete",
    "research_source_complete", "lessons", "research", "snapshot_digest",
})
_CROSS_RUN_CLAIM_SEGMENT_KEYS = frozenset({
    "read_complete", "rows_total", "rows_retained", "rows_quarantined",
    "malformed_rows", "invalid_rows",
})
_CROSS_RUN_GOVERNANCE_KEYS = frozenset({
    "v", "status", "complete", "code", "ledger", "reason",
})
_CROSS_RUN_GOVERNANCE_LEDGERS = frozenset({
    "concept_aliases", "concept_splits", "claim_decisions", "concept_governance",
    "concept_capsules", "cross_run_sources", "concept_curation", "claim_curation",
    "task_facets", "task_facets_curation",
})
_CROSS_RUN_GOVERNANCE_REASONS = frozenset({
    "storage_unreadable", "torn_tail", "blank_row", "malformed_json", "non_object",
    "unsupported_schema", "unknown_action", "invalid_record", "duplicate_action_id",
    "invalid_revision", "revision_mismatch", "revision_collision", "identity_cycle",
})
_MAX_CROSS_RUN_RECEIPT_COUNT = (1 << 31) - 1


def bounded_cross_run_advisory_receipt(value) -> dict:
    """Return the exact bounded audit receipt stamped by proposal cues, or ``{}``.

    Staged Cards may survive a process restart before their Node is built, so the proposal's advisory
    provenance must ride the durable Card receipt rather than a process-local role attribute.  This
    boundary is deliberately narrower than an arbitrary JSON copier: only the two current v2 shapes
    (available corpus or governance-unavailable) pass, with a bounded nested receipt payload.
    """

    if not isinstance(value, dict) or not value or value.get("v") != 2:
        return {}

    def _digest(item) -> bool:
        return valid_digest_ref(item)          # bare 64-hex: the same predicate, empty namespace

    def _count(item) -> bool:
        return type(item) is int and 0 <= item <= _MAX_CROSS_RUN_RECEIPT_COUNT

    def _scope_identity(item) -> bool:
        if not isinstance(item, str) or len(item) > 500:
            return False
        # The proposal writer uses the same always-on durable boundary. Requiring a fixed point
        # makes replay reject credential/control-bearing strings instead of silently changing the
        # audit receipt whose digests describe the model-visible corpus.
        clean = redact_persisted_text(
            item, max_chars=500, entropy=False, single_line=True,
        )
        return item == " ".join(clean.split())

    def _concept_scope(item) -> dict | None:
        if not isinstance(item, dict) or set(item) != _CROSS_RUN_CONCEPT_SCOPE_KEYS:
            return None
        if type(item.get("scope_complete")) is not bool:
            return None
        count_keys = _CROSS_RUN_CONCEPT_SCOPE_KEYS - {"scope_complete"}
        if any(not _count(item.get(key)) for key in count_keys):
            return None
        unknown = item["scope_unknown_capsules"]
        if (item["scope_complete"] != (unknown == 0)
                or item["scope_fingerprint_unknown_capsules"] > unknown
                or item["scope_direction_unknown_capsules"] > unknown
                or (unknown == 0 and item["scope_fingerprint_items_omitted"] != 0)):
            return None
        return {key: item[key] for key in (
            "scope_complete", "scope_unknown_capsules",
            "scope_fingerprint_unknown_capsules", "scope_fingerprint_items_omitted",
            "scope_direction_unknown_capsules",
        )}

    def _claim_segment(item) -> dict | None:
        if not isinstance(item, dict) or set(item) != _CROSS_RUN_CLAIM_SEGMENT_KEYS:
            return None
        if type(item.get("read_complete")) is not bool:
            return None
        count_keys = _CROSS_RUN_CLAIM_SEGMENT_KEYS - {"read_complete"}
        if any(not _count(item.get(key)) for key in count_keys):
            return None
        if (item["rows_quarantined"] != item["malformed_rows"] + item["invalid_rows"]
                or item["rows_total"] != item["rows_retained"] + item["rows_quarantined"]
                or item["read_complete"] != (item["rows_quarantined"] == 0)):
            return None
        return {key: item[key] for key in (
            "read_complete", "rows_total", "rows_retained", "rows_quarantined",
            "malformed_rows", "invalid_rows",
        )}

    def _claim_source(item) -> dict | None:
        if not isinstance(item, dict) or set(item) != _CROSS_RUN_CLAIM_SOURCE_KEYS:
            return None
        bool_keys = (
            "receipt_known", "source_complete", "read_complete",
            "research_source_complete",
        )
        if item.get("v") != 1 or any(type(item.get(key)) is not bool for key in bool_keys):
            return None
        lessons = _claim_segment(item.get("lessons"))
        research = _claim_segment(item.get("research"))
        snapshot_digest = item.get("snapshot_digest")
        if lessons is None or research is None:
            return None
        if not ((item["receipt_known"] is False and snapshot_digest == "")
                or (item["receipt_known"] is True and _digest(snapshot_digest))):
            return None
        read_complete = lessons["read_complete"] and research["read_complete"]
        consistent = (
            (not item["receipt_known"]
             and not item["source_complete"]
             and not item["read_complete"]
             and not item["research_source_complete"])
            or (
                item["receipt_known"]
                and item["read_complete"] == read_complete
                and (not item["research_source_complete"] or research["read_complete"])
                and item["source_complete"]
                == (lessons["read_complete"] and item["research_source_complete"])
            )
        )
        if not consistent:
            return None
        return {
            "v": 1,
            **{key: item[key] for key in bool_keys},
            "lessons": lessons,
            "research": research,
            "snapshot_digest": snapshot_digest,
        }

    def _governance(item) -> dict | None:
        if not isinstance(item, dict) or set(item) != _CROSS_RUN_GOVERNANCE_KEYS:
            return None
        if not (
            item.get("v") == 1
            and item.get("status") == "unavailable"
            and item.get("complete") is False
            and item.get("code") == "governance_ledger_unavailable"
            and item.get("ledger") in _CROSS_RUN_GOVERNANCE_LEDGERS
            and item.get("reason") in _CROSS_RUN_GOVERNANCE_REASONS
        ):
            return None
        return {key: item[key] for key in (
            "v", "status", "complete", "code", "ledger", "reason",
        )}

    if set(value) == _CROSS_RUN_UNAVAILABLE_KEYS:
        governance = _governance(value.get("governance"))
        if (value.get("status") != "unavailable"
                or value.get("complete") is not False
                or governance is None):
            return {}
        return {
            "v": 2,
            "status": "unavailable",
            "complete": False,
            "governance": governance,
        }

    if set(value) != _CROSS_RUN_AVAILABLE_KEYS:
        return {}
    concept_scope = _concept_scope(value.get("concept_scope"))
    claim_source = _claim_source(value.get("claim_source"))
    if (concept_scope is None or claim_source is None
            or not all(_count(value.get(key))
                       for key in ("n_lessons", "n_capsules", "n_research"))
            or not _scope_identity(value.get("scope_task"))
            or not _scope_identity(value.get("excluded_run"))
            or not _digest(value.get("corpus_digest"))
            or not _digest(value.get("render_digest"))):
        return {}
    return {
        "v": 2,
        "scope_task": value["scope_task"],
        "excluded_run": value["excluded_run"],
        "n_lessons": value["n_lessons"],
        "n_capsules": value["n_capsules"],
        "n_research": value["n_research"],
        "concept_scope": concept_scope,
        "claim_source": claim_source,
        "corpus_digest": value["corpus_digest"],
        "render_digest": value["render_digest"],
    }


def valid_advisory_ref(value, namespace: str) -> bool:
    """Whether ``value`` is one exact, printable, content-addressed advisory reference.

    Cards expose these identifiers in the tokenless public dump, so accepting an arbitrary string would
    recreate the body/path side channel the ref-only Card contract is intended to close.
    """
    prefix = _ADVISORY_REF_PREFIXES.get(namespace) if isinstance(namespace, str) else None
    # An UNKNOWN namespace stays a refusal before the shared predicate is consulted: `prefix=None`
    # would otherwise have to mean "no prefix", which is the bare-64-hex case and would admit a
    # digest under a namespace this module does not issue.
    return prefix is not None and valid_digest_ref(value, prefix=prefix)


def stable_advisory_ref(namespace: str, payload) -> str | None:
    """Return ``<namespace>:sha256:<digest>`` over deterministic bounded JSON, or ``None``.

    Callers pass their already-sanitized, deliberately small identity projection.  ``allow_nan=False``
    and a strict namespace list make malformed/future values fail closed instead of minting unstable ids.
    """
    prefix = _ADVISORY_REF_PREFIXES.get(namespace) if isinstance(namespace, str) else None
    if prefix is None:
        return None
    # The strict namespace list above is this function's own contribution; the encode/hash tail is the
    # shared one (doc 25 CO-08). Deliberately UNCAPPED, unlike the two agent-output minters: callers
    # pass an already-sanitized, deliberately small identity projection, so a size refusal here could
    # only drop a well-formed advisory.
    return canonical_json_digest(payload, prefix=prefix)


def research_memo_ref(payload) -> str | None:
    """Stable id for the canonical persisted memo, excluding its self-referential child ids."""
    clean = sanitize_research_memo_payload(payload)
    clean.pop("memo_id", None)
    for claim in clean.get("claims", []):
        if isinstance(claim, dict):
            claim.pop("claim_id", None)
    return stable_advisory_ref("memo", clean)


def research_claim_ref(memo_id: str, index: int, claim) -> str | None:
    """Stable, position-aware id for one claim in an exact persisted memo."""
    if not valid_advisory_ref(memo_id, "memo") or type(index) is not int or not 0 <= index < 64:
        return None
    if not isinstance(claim, dict):
        return None
    bounded = {
        key: claim[key]
        for key in ("statement", "node_ids", "urls", "url_identities", "evidence_receipt")
        if key in claim
    }
    return stable_advisory_ref(
        "claim", {"memo_id": memo_id, "index": index, "claim": bounded})


def research_lesson_ref(lesson, evidence_refs) -> str | None:
    """Stable id for a distilled lesson bound to the exact cited node lifecycles."""
    if not isinstance(lesson, dict) or not isinstance(evidence_refs, list) or len(evidence_refs) > 64:
        return None
    refs = []
    for ref in evidence_refs:
        if (not isinstance(ref, dict) or set(ref) != {"node_id", "generation"}
                or type(ref.get("node_id")) is not int or ref["node_id"] < 0
                or type(ref.get("generation")) is not int or ref["generation"] < 0):
            return None
        refs.append({"node_id": ref["node_id"], "generation": ref["generation"]})
    statement = lesson.get("statement")
    outcome = lesson.get("outcome")
    stance = lesson.get("claim_stance")
    if (not isinstance(statement, str) or not isinstance(outcome, str)
            or (stance is not None and not isinstance(stance, str))):
        return None
    identity = {
        "statement": statement[:4_000],
        "outcome": outcome[:80],
        "claim_stance": stance[:80] if stance is not None else None,
        "evidence_refs": refs,
    }
    return stable_advisory_ref("lesson", identity)


def research_lesson_receipt(lesson, state) -> dict:
    """Project one existing lesson event row plus an exact, lifecycle-bound opaque id.

    The event already carries the human-readable lesson for its audit timeline.  The additive
    ``lesson_id``/``evidence_refs`` members are the only pieces the Card enrichment writer consumes.
    Missing or stale evidence deliberately produces no id, so a numeric node slot can never re-home an
    old lesson after reset/retry.
    """
    raw = lesson if isinstance(lesson, dict) else {}
    row = {
        "statement": raw.get("statement", ""),
        "outcome": raw.get("outcome", ""),
        "claim_stance": raw.get("claim_stance"),
        "evidence": raw.get("evidence"),
    }
    raw_evidence = raw.get("evidence")
    if not isinstance(raw_evidence, (list, tuple)) or len(raw_evidence) > 64:
        return row
    evidence_refs = []
    seen: set[int] = set()
    nodes = getattr(state, "nodes", {}) if state is not None else {}
    aborted = getattr(state, "aborted_nodes", set()) if state is not None else set()
    for raw_node_id in raw_evidence:
        if type(raw_node_id) is not int or raw_node_id < 0 or raw_node_id in seen:
            return row
        node = nodes.get(raw_node_id)
        if (node is None or getattr(node, "tombstoned", False) or raw_node_id in aborted
                or type(getattr(node, "attempt", None)) is not int
                or getattr(node, "idea", None) is None):
            return row
        seen.add(raw_node_id)
        evidence_refs.append({"node_id": raw_node_id, "generation": node.attempt})
    lesson_id = research_lesson_ref(raw, evidence_refs)
    if lesson_id is not None:
        row["lesson_id"] = lesson_id
        row["evidence_refs"] = evidence_refs
    return row


def _bounded_source(value) -> tuple[tuple | list, int, bool]:
    """Return a bounded-contract source plus its observable cardinality.

    A wrong-shaped non-null value is one opaque omitted item, not an authoritative empty list.  This
    distinction is what lets a second sanitizer/finalizer fail closed instead of laundering malformed
    model output into a complete zero-row receipt.
    """
    if isinstance(value, (list, tuple)):
        return value, len(value), True
    return (), int(value is not None), value is None


def _count_receipt(raw, *, total: int, retained: int, prefix: str = "") -> dict:
    """Build an idempotent total/omission receipt, preserving a prior canonical denominator."""
    total_key = f"{prefix}total"
    retained_key = f"{prefix}retained"
    omitted_key = f"{prefix}omitted"
    declared_total = raw.get(total_key) if isinstance(raw, dict) else None
    declared_retained = raw.get(retained_key) if isinstance(raw, dict) else None
    declared_omitted = raw.get(omitted_key) if isinstance(raw, dict) else None
    declared_complete = raw.get("complete") if isinstance(raw, dict) else None
    canonical = (
        raw.get("v") == RESEARCH_RECEIPT_VERSION
        and type(declared_total) is int and 0 <= declared_total <= _MAX_ADVISORY_COUNT
        and type(declared_retained) is int and 0 <= declared_retained <= declared_total
        and declared_retained == total
        and type(declared_omitted) is int and 0 <= declared_omitted <= declared_total
        and declared_omitted == declared_total - total
        and type(declared_complete) is bool
        and declared_complete == (declared_omitted == 0)
    ) if isinstance(raw, dict) else False
    source_total = declared_total if canonical else total
    omitted = max(0, source_total - retained)
    return {
        "v": RESEARCH_RECEIPT_VERSION,
        total_key: source_total,
        retained_key: retained,
        omitted_key: omitted,
        "complete": omitted == 0,
    }


def research_claims_receipt(payload) -> dict | None:
    """Return a canonical memo claim receipt, or ``None`` for legacy/malformed metadata."""
    if not isinstance(payload, dict):
        return None
    claims, current_total, shape_known = _bounded_source(payload.get("claims"))
    raw = payload.get("claims_receipt")
    receipt = _count_receipt(raw, total=current_total, retained=len(claims))
    if not shape_known or raw != receipt:
        return None
    return receipt


def research_evidence_receipt(claim) -> dict | None:
    """Return a canonical per-claim evidence receipt, or ``None`` for a legacy claim."""
    if not isinstance(claim, dict):
        return None
    nodes, node_total, node_shape_known = _bounded_source(claim.get("node_ids"))
    urls, url_total, url_shape_known = _bounded_source(claim.get("urls"))
    raw = claim.get("evidence_receipt")
    if not isinstance(raw, dict) or raw.get("v") != RESEARCH_RECEIPT_VERSION:
        return None
    node_receipt = _count_receipt(
        raw, total=node_total, retained=len(nodes), prefix="node_refs_")
    url_receipt = _count_receipt(
        raw, total=url_total, retained=len(urls), prefix="url_refs_")
    expected = {
        "v": RESEARCH_RECEIPT_VERSION,
        **{key: value for key, value in node_receipt.items() if key not in ("v", "complete")},
        **{key: value for key, value in url_receipt.items() if key not in ("v", "complete")},
        "complete": node_receipt["complete"] and url_receipt["complete"],
    }
    if not node_shape_known or not url_shape_known or raw != expected:
        return None
    return expected


def _text(value, cap: int, budget: list[int], *, single_line: bool = False) -> str:
    room = min(max(0, int(cap)), budget[0])
    if room <= 0:
        return ""
    clean = redact_persisted_text(
        value, max_chars=room, entropy=True, single_line=single_line)
    budget[0] -= len(clean)
    return clean


def _items(value, maximum: int):
    return itertools.islice(value, maximum) if isinstance(value, (list, tuple)) else ()


def _source_url(value, persisted_identity, budget: list[int]) -> tuple[str, str]:
    """Project one URL as safe display text plus its stable opaque evidence identity."""
    ref = canonical_source_ref(value, persisted_identity=persisted_identity)
    if ref is None:
        # Backward compatibility for non-HTTP legacy labels: they remain visible but cannot become
        # verifier evidence merely by colliding with an HTTP source identity.
        return _text(value, 1_600, budget, single_line=True), ""
    if budget[0] <= len(ref.identity):
        return "", ""
    budget[0] -= len(ref.identity)
    display = _text(ref.display_url, 1_600, budget, single_line=True)
    if not display:
        budget[0] += len(ref.identity)
        return "", ""
    return display, ref.identity


def _tree(value, budget: list[int], items: list[int], depth: int = 0):
    """Bound and redact one untrusted advisory subtree, spending the SHARED page budget.

    The walk is `core/redact.py::bounded_redacted_tree`, shared with the span/trace sanitizer (doc 25
    CO-06). This projection caps each string at 2 000 rather than letting one field spend the whole
    page: an advisory payload is a LIST of rows a human reads, and an oversized early statement must
    not starve the later ones.
    """
    return bounded_redacted_tree(value, budget, items, max_items=64, max_depth=5,
                                 str_cap=2_000, key_cap=128, depth=depth)


def _verification(value, budget: list[int], items: list[int]):
    """Project the verifier's indexed verdict contract without starving late rows.

    A generic depth-first tree projection lets a few oversized early statements consume the whole
    allowance and silently drop a later ``unsupported`` verdict. Verdict order is also positional with
    memo claims, so sorting warnings first would corrupt the contract. Give every bounded row a fair
    share instead; keep the generic legacy-tree behavior for non-contract verification payloads.
    """
    if not isinstance(value, dict) or not isinstance(value.get("verdicts"), (list, tuple)):
        return _tree(value, budget, items)

    raw_verdicts = value["verdicts"]
    raw_total = min(len(raw_verdicts), _MAX_ADVISORY_COUNT)
    declared_total = value.get("total_verdicts")
    declared_omitted = value.get("omitted_verdicts")
    # Writer and replay boundaries both sanitize the memo. Preserve an earlier canonical omission
    # receipt only when both bounded counters agree exactly with the rows now present; inconsistent
    # provider aggregates can never conceal rows or turn a complete check into a trusted one.
    metadata_is_canonical = (
        type(declared_total) is int and 0 <= declared_total <= _MAX_ADVISORY_COUNT
        and type(declared_omitted) is int and 0 <= declared_omitted <= _MAX_ADVISORY_COUNT
        and declared_total >= raw_total
        and declared_omitted == declared_total - raw_total
    )
    total_verdicts = declared_total if metadata_is_canonical else raw_total
    raw_rows = list(itertools.islice(raw_verdicts, _MAX_VERIFICATION_VERDICTS))
    method = _text(value.get("method", "unknown"), 64, budget, single_line=True) or "unknown"
    verdicts = []
    for index, raw in enumerate(raw_rows):
        remaining_rows = len(raw_rows) - index
        # Equal-share allocation preserves every positional verdict under the aggregate cap. The note
        # precedes the duplicated statement so the verifier's reason survives tight legacy payloads.
        allowance = budget[0] // remaining_rows if remaining_rows else 0
        row_budget = [allowance]
        row = raw if isinstance(raw, dict) else {}
        candidate = _text(row.get("verdict", "unclear"), 32, row_budget,
                          single_line=True).lower()
        verdict = candidate if candidate in _VERDICTS else "unclear"
        note = _text(row.get("note", ""), min(200, row_budget[0]), row_budget,
                     single_line=True)
        statement = _text(row.get("statement", ""), min(1_600, row_budget[0]), row_budget)
        budget[0] -= allowance - row_budget[0]
        raw_evidence = row.get("evidence")
        evidence = {"v": RESEARCH_RECEIPT_VERSION, "node_refs": [],
                    "url_identities": [], "complete": False}
        if isinstance(raw_evidence, dict) and raw_evidence.get("v") == RESEARCH_RECEIPT_VERSION:
            raw_nodes = raw_evidence.get("node_refs")
            raw_urls = raw_evidence.get("url_identities")
            if isinstance(raw_nodes, (list, tuple)) and isinstance(raw_urls, (list, tuple)):
                for ref in raw_nodes[:MAX_RESEARCH_NODE_REFS]:
                    if (isinstance(ref, dict) and type(ref.get("node_id")) is int
                            and ref["node_id"] >= 0 and type(ref.get("generation")) is int
                            and ref["generation"] >= 0):
                        evidence["node_refs"].append({
                            "node_id": ref["node_id"], "generation": ref["generation"]})
                evidence["url_identities"] = [
                    identity for identity in raw_urls[:MAX_RESEARCH_URL_REFS]
                    if valid_source_identity(identity)
                ]
                evidence["complete"] = bool(
                    raw_evidence.get("complete") is True
                    and len(evidence["node_refs"]) == len(raw_nodes)
                    and len(evidence["url_identities"]) == len(raw_urls)
                )
        verdicts.append({"statement": statement, "verdict": verdict, "note": note,
                         "evidence": evidence})

    return {
        "verdicts": verdicts,
        "method": method,
        # Recompute the aggregate from the bounded positional rows; never persist a conflicting
        # model/provider aggregate beside the verdicts the operator can actually inspect.
        "unsupported": sum(row["verdict"] == "unsupported" for row in verdicts),
        # These counts describe the pre-cap positional contract. They survive the second sanitizer
        # pass so the UI never mistakes a durable 64-row projection for a complete verification.
        "total_verdicts": total_verdicts,
        "omitted_verdicts": max(0, total_verdicts - len(verdicts)),
    }


#: The EXACT key set `_verification` above writes, named once so a READER can be checked against it.
#: `trust/memo_verify.py::verify_memo` (the origin writer) emits the same five and nothing else, and
#: `tests/test_research_memo_verdicts.py` re-derives BOTH from their own source rather than trusting
#: this constant — a hand-maintained copy of a contract is the defect this constant exists to stop.
VERIFICATION_BLOCK_KEYS = frozenset({
    "verdicts", "method", "unsupported", "total_verdicts", "omitted_verdicts",
})

#: What a reader may say about ONE claim. `unverified` is the reader's own word and is deliberately
#: NOT in `_VERDICTS`: no writer may emit it, it is what a reader says when the block has no verdict
#: for this claim (bounded away, or an alignment mismatch). `engine/lessons.py` spells the same word
#: for the same two conditions on the durable D8 path.
VERDICT_UNVERIFIED = "unverified"


def memo_verification_view(memo) -> dict:
    """The READ side of the verification block `_verification` above writes — a memo's verifier
    result, per claim, in a shape a renderer can print without re-deriving the contract.

    IT LIVES BESIDE ITS WRITER ON PURPOSE. `tools/run_tools.py::read_research_memo` keyed on
    `verification["summary"]` from the day it was written (`f180c986`, 2026-07-10) while the writer
    at that same commit returned `{"verdicts", "method", "unsupported"}` — so the branch was dead on
    arrival and **not one verifier verdict has ever reached a role through that tool**. Measured over
    every `research_completed` row in `runs/` on 2026-08-16: 102 memos, 98 carrying a verification
    block, **0 carrying a `summary` key**, 98 carrying none, 16 with every verdict `unsupported`.
    The key could not have appeared even by accident: `sanitize_research_memo_payload` runs on the
    write path AND at replay (`events/replay.py`), and `_verification` rebuilds the block as a dict
    literal, so anything the origin writer did not emit is dropped before a reader ever sees it.
    A reader one file away from the writer is a reader that goes red in the same diff.

    THE JOIN, and why it is not just an index. A verdict row carries its own `statement`, and this
    repo already has three consumers that pair claim `i` with verdict `i` and then REQUIRE the two
    statements to be equal — `engine/lessons.py` (durable D8 claims; a mismatch becomes
    `unverified`/"verification alignment mismatch"), `engine/research_cadence.py` (Card enrichment;
    a mismatch is skipped) and `ui/src/researchMemoModel.js::alignVerification` (the operator's own
    memo card). Measured over the corpus: 98/98 blocks are index-aligned and 833/833 verdict rows
    match their claim's statement exactly, so the rule costs nothing today and is the only thing
    stopping a bounded or reordered block from printing one claim's refusal under another claim's
    text. Those three are NOT routed through here yet — two of them write durable cross-run records
    and re-pointing them is not a change to make in the same hour a run launches (`docs/BACKLOG.md`
    §0.7). This is the fourth implementation of that rule and the first one a test binds to a writer.

    Returns `{"status", "method", "counts", "rows"}`:
      * `status` — `"absent"` (no block: the memo was never verified, or had no claims to verify),
        `"malformed"` (a block that is not the contract shape), or `"present"`.
      * `rows` — one per CLAIM, in claim order, `{"verdict", "note", "statement", "aligned"}`.
        A claim with no verdict row, or one whose statement disagrees, is `VERDICT_UNVERIFIED`.
      * `counts` — per-verdict tallies over the rows actually present, plus the block's own
        `total_verdicts`/`omitted_verdicts` receipt so a bounded check cannot read as a complete one.

    Pure and total: it reads a dict and returns a dict, it never raises, and it decides nothing.
    """
    if not isinstance(memo, dict):
        return {"status": "absent", "method": "", "counts": {}, "rows": []}
    block = memo.get("verification")
    # THE JOIN IS POSITIONAL, so both sides must enumerate the SAME population. The writer
    # (`trust/memo_verify.py::_check_claims`) emits one row per claim of the sanitized list,
    # dict-coercing a non-dict and filtering NOTHING — and `sanitize_research_memo_payload` keeps a
    # whitespace-only statement verbatim (`redact_persisted_text(" ") == " "`). Dropping blanks
    # HERE therefore shifted the join by one for every blank above: each later claim read
    # `unverified` / "verification alignment mismatch" while its real verdict was counted as an
    # unmatched row, and that false tally went on into `verdict_tally` / `memo_verdict_cue` prompts
    # (driven: claims [" ", "A", "B"] made both real claims `unverified` with
    # `unmatched_verdicts` 1). So the population is the writer's — same coercion, same cap — and
    # blankness is a DISPLAY concern, applied after the rows are paired.
    claims = [c if isinstance(c, dict) else {}
              for c in (memo.get("claims") or [])[:MAX_RESEARCH_CLAIMS]]
    if block is None:
        return {"status": "absent", "method": "", "counts": {}, "rows": []}
    if not isinstance(block, dict) or not isinstance(block.get("verdicts"), (list, tuple)):
        # A block that is not the contract shape is NOT silently treated as "no block": absence and
        # a broken check have different remedies, and reading the second as the first is this whole
        # defect one layer down. `_verification` routes exactly this case to the legacy `_tree`
        # projection, so a memo can reach a reader carrying arbitrary keys.
        return {"status": "malformed", "method": "", "counts": {}, "rows": []}

    raw_rows = [row if isinstance(row, dict) else {} for row in block["verdicts"]]
    rows: list[dict] = []
    for index, claim in enumerate(claims):
        statement = str(claim.get("statement") or "").strip()
        if not statement:
            # A blank claim holds its POSITION in the join — that is the whole point — and is then
            # dropped from what a reader sees and counts. It is not a claim; it is an artefact of a
            # sanitizer that preserves whitespace, and neither the tallies nor the prompts should
            # ever have to know it existed.
            continue
        row = raw_rows[index] if index < len(raw_rows) else None
        if row is None:
            rows.append({"verdict": VERDICT_UNVERIFIED, "aligned": False, "statement": statement,
                         "note": "no verifier verdict was recorded for this claim"})
            continue
        if str(row.get("statement") or "").strip() != statement:
            rows.append({"verdict": VERDICT_UNVERIFIED, "aligned": False, "statement": statement,
                         "note": "verification alignment mismatch"})
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        rows.append({
            "verdict": verdict if verdict in _VERDICTS else VERDICT_UNVERIFIED,
            "aligned": True,
            "statement": statement,
            "note": str(row.get("note") or "").strip(),
        })
    # Verdict rows BEYOND the claim list are counted and never dropped in silence: a block longer
    # than its claims is a mismatch the reader must be able to say out loud. Measured against the
    # POSITIONS, not against the rendered rows — a blank claim consumed a position and its row is
    # therefore matched, not spare.
    unmatched = max(0, len(raw_rows) - len(claims))

    counts = {name: sum(1 for row in rows if row["verdict"] == name)
              for name in sorted(_VERDICTS | {VERDICT_UNVERIFIED})}
    counts["claims"] = len(rows)
    counts["unmatched_verdicts"] = unmatched
    declared_total = block.get("total_verdicts")
    counts["total_verdicts"] = declared_total if type(declared_total) is int else len(raw_rows)
    declared_omitted = block.get("omitted_verdicts")
    counts["omitted_verdicts"] = declared_omitted if type(declared_omitted) is int else 0
    method = str(block.get("method") or "").strip() or "unknown"
    return {"status": "present", "method": method[:64], "counts": counts, "rows": rows}


#: The ORDER and the WORDING every surface tallies a verification block in. It is one table because
#: the memo now reaches a role through TWO channels — the `read_research_memo` tool (PULL) and the
#: `roles.py::_state_brief` résumé (PUSH) — and a role that sees both must not have to reconcile two
#: vocabularies for the same block. `unsupported` leads because it is the only bucket a reader must
#: act on; zero-count buckets are dropped so the tally carries information wherever it appears.
_VERDICT_TALLY_LABELS: tuple[tuple[str, str], ...] = (
    ("unsupported", "UNSUPPORTED"), ("supported", "supported"),
    ("unclear", "unclear"), ("cited", "cited-but-unjudged"),
    (VERDICT_UNVERIFIED, "unverified"),
)


def verdict_tally(counts) -> str:
    """`"6 UNSUPPORTED, 2 supported"` from a `memo_verification_view` counts mapping. `""` when the
    block reports nothing at all — a caller says what an empty tally means in its own words, because
    "nothing to report" and "no claims" are different sentences on the two surfaces that print it.

    Pure and total; a non-mapping reads as empty rather than raising, since both callers are on
    never-raise paths (a tool answer and a prompt brief)."""
    if not isinstance(counts, dict):
        return ""
    return ", ".join(f"{counts.get(name, 0)} {label}" for name, label in _VERDICT_TALLY_LABELS
                     if counts.get(name, 0))


def memo_verdict_cue(memo) -> str:
    """ONE bracketed clause qualifying a memo SUMMARY that is being pushed into a prompt. `""` never
    happens for a dict — absence and unreadability get sentences of their own, because this whole
    defect family is a missing check being read as a passing one.

    WHY THE SUMMARY NEEDS ITS OWN SENTENCE, and it is the sharpest fact here: `trust/memo_verify.py::
    verify_memo` verifies `memo["claims"]` and returns `None` when there are none. **It never looks at
    `memo["summary"]` at any commit.** So the field `roles.py::_state_brief` pushes into every
    Researcher, crash-triage and repair-critic prompt is the one field of the memo that no verifier
    has ever checked — the verdict is not merely elsewhere in the payload, it does not exist for this
    text. That is why the clause states the CLAIM tally *and* says the summary is outside it, rather
    than implying the tally covers what follows it.

    WHAT IT COST, on the record. `rubertlite-dr-unified-v8`'s `at_node: 0` memo records
    `total_verdicts: 8, unsupported: 8` — verdict[0] refusing a `recall@100=0.8776` claim with
    `cited experiments do not exist: [9]`, a node id belonging to `rubert-dr-0807`, a run
    `engine/eval_contract.py` reports as a DIFFERENT evaluation contract (different eval command,
    different declared paths). Its summary opens *"…then climb from the known ~0.88 plateau"*, and
    measured over that run's own `spans.jsonl` the pushed line reached **293 real prompts** (propose
    269, triage 20, repair_critic 4) carrying that rounded foreign plateau in 52 of them, with the
    word `unsupported` and the word `Verifier` appearing NOWHERE in any of the 293 whole prompts.
    Note what that measurement also REFUTES: the literal `0.8776` is in 11 of the run's 15 full memo
    summaries but in **0** of the 300-char windows `_state_brief` actually pushes, so the pushed
    carrier was the rounded `~0.88`, not the number the residue note named.

    IT ANNOTATES AND WITHHOLDS NOTHING, the same line `tools/run_tools.py::_verifier_lead` holds one
    channel over. Suppressing an unsupported memo's summary was weighed and refused on the corpus:
    26 of the 100 pushable memos in `runs/` have NO supported verdict at all, and 45 of the 45
    verdicts the DETERMINISTIC pass emits are `unsupported` about the CITATION — a fact about the
    footnote, not the claim. Dropping a real finding because its footnote is bad is worse than the
    defect. It also states no opinion about which number is foreign: a summary is prose with no
    per-number provenance, and deciding that from the model's own text is what docs/36 forbids.

    Pure, total, and it decides nothing: a string built from a folded `RunState` payload."""
    view = memo_verification_view(memo)
    status = view.get("status")
    if status == "absent":
        return (" [VERIFIER: NOT RUN on this memo's claims — absence of a verdict is not a pass, "
                "and nothing verifies the summary itself]")
    if status == "malformed":
        return (" [VERIFIER: result RECORDED BUT UNREADABLE for this memo — treat its claims as "
                "unchecked; nothing verifies the summary itself]")
    counts = view.get("counts") or {}
    tally = verdict_tally(counts) or "nothing to report"
    return (f" [VERIFIER on this memo's {counts.get('claims', 0)} claim(s): {tally}; "
            "nothing verifies the summary itself]")


def sanitize_research_memo_payload(payload, *, add_receipts: bool = True) -> dict:
    """Canonicalize a model-, tool-, or legacy-event research memo."""
    src = payload if isinstance(payload, dict) else {}
    budget = [_MAX_ADVISORY_TEXT]
    verification_items = [_MAX_TREE_ITEMS // 2]
    proposal_items = [_MAX_TREE_ITEMS // 2]
    out = {
        "summary": _text(src.get("summary", ""), 4_000, budget),
        "reasoning": "",
        "findings": [],
        "claims": [],
        "sources": [],
        "recommended_directions": [],
        "open_questions": [],
        "next_experiments": [],
        "proposed_ideas": [],
        "at_node": (src.get("at_node") if type(src.get("at_node")) is int
                    and 0 <= src.get("at_node") <= (1 << 63) - 1 else None),
        "trigger": _text(src.get("trigger", ""), 64, budget, single_line=True),
    }
    if valid_advisory_ref(src.get("memo_id"), "memo"):
        out["memo_id"] = src["memo_id"]
    if "verification" in src:
        # Reserve a bounded slice for trust output before model narrative/proposals. The shared 64k
        # cap must not persist recommendations while silently erasing unsupported verdicts.
        allowance = min(_MAX_VERIFICATION_TEXT, budget[0])
        verification_budget = [allowance]
        out["verification"] = _verification(
            src["verification"], verification_budget, verification_items)
        budget[0] -= allowance - verification_budget[0]
    raw_claims, claims_total, claims_shape_known = _bounded_source(src.get("claims"))
    for claim in itertools.islice(raw_claims, MAX_RESEARCH_CLAIMS):
        if not isinstance(claim, dict):
            continue
        statement = _text(claim.get("statement", ""), 1_600, budget)
        raw_nodes, node_total, node_shape_known = _bounded_source(claim.get("node_ids"))
        raw_urls_source, url_total, url_shape_known = _bounded_source(claim.get("urls"))
        raw_urls = list(itertools.islice(raw_urls_source, MAX_RESEARCH_URL_REFS))
        raw_identities = list(_items(claim.get("url_identities"), MAX_RESEARCH_URL_REFS))
        urls = []
        url_identities = []
        for index, value in enumerate(raw_urls):
            persisted = raw_identities[index] if index < len(raw_identities) else None
            display, identity = _source_url(value, persisted, budget)
            if display and identity:
                urls.append(display)
                url_identities.append(identity)
        node_ids = [n for n in itertools.islice(raw_nodes, MAX_RESEARCH_NODE_REFS)
                    if type(n) is int and 0 <= n <= (1 << 63) - 1]
        prior_evidence = claim.get("evidence_receipt")
        node_receipt = _count_receipt(
            prior_evidence, total=node_total, retained=len(node_ids), prefix="node_refs_")
        url_receipt = _count_receipt(
            prior_evidence, total=url_total, retained=len(urls), prefix="url_refs_")
        evidence_receipt = {
            "v": RESEARCH_RECEIPT_VERSION,
            **{key: value for key, value in node_receipt.items() if key not in ("v", "complete")},
            **{key: value for key, value in url_receipt.items() if key not in ("v", "complete")},
            "complete": (node_shape_known and url_shape_known
                         and node_receipt["complete"] and url_receipt["complete"]),
        }
        projected_claim = {
            "statement": statement,
            "node_ids": node_ids,
            "urls": urls,
            "url_identities": url_identities,
        }
        if valid_advisory_ref(claim.get("claim_id"), "claim"):
            projected_claim["claim_id"] = claim["claim_id"]
        if add_receipts or "evidence_receipt" in claim:
            projected_claim["evidence_receipt"] = evidence_receipt
        out["claims"].append(projected_claim)
    claims_receipt = _count_receipt(
        src.get("claims_receipt"), total=claims_total, retained=len(out["claims"]))
    if not claims_shape_known:
        claims_receipt["complete"] = False
    if add_receipts or "claims_receipt" in src:
        out["claims_receipt"] = claims_receipt
    for source in _items(src.get("sources"), MAX_RESEARCH_SOURCES):
        if not isinstance(source, dict):
            continue
        title = _text(source.get("title", ""), 400, budget, single_line=True)
        display_url, url_identity = _source_url(
            source.get("url", ""), source.get("url_identity"), budget)
        out["sources"].append({
            "title": title,
            "url": display_url,
            "url_identity": url_identity,
            "snippet": _text(source.get("snippet", ""), 200, budget),
        })
    out["reasoning"] = _text(src.get("reasoning", ""), 12_000, budget)
    out["findings"] = [_text(v, 1_200, budget) for v in _items(src.get("findings"), 32)]
    # The compat field and the two halves it was split into share ONE bound and one text rule: a
    # reader putting them side by side must not find one clipped where the other was not. See
    # `ResearchMemo.open_questions` for why the split exists — the old field's NAME contradicted its
    # own description, so a concrete experiment arrived through a channel that carries no action.
    for _field in ("recommended_directions", "open_questions", "next_experiments"):
        out[_field] = [
            _text(v, 1_200, budget, single_line=True)
            for v in _items(src.get(_field), 16)
        ]
    out["proposed_ideas"] = [
        _tree(v, budget, proposal_items) for v in _items(src.get("proposed_ideas"), 16)
    ]
    return out


_REPORT_LIST_FIELDS = ("caveats", "what_worked", "learnings", "what_didnt", "next_directions")
_LEGACY_REPORT_FAILURE = "(report generation failed:"


def _report_verdict(value):
    """Collapse the exact legacy raw-exception envelope before ordinary text redaction."""
    if isinstance(value, str) and value.lstrip().lower().startswith(_LEGACY_REPORT_FAILURE):
        return "(report generation failed: The model provider returned an error.)"
    return value


def sanitize_report_payload(payload) -> dict:
    """Canonicalize a generated or legacy run-report payload."""
    src = payload if isinstance(payload, dict) else {}
    budget = [_MAX_ADVISORY_TEXT]
    out = {
        "headline": _text(src.get("headline", ""), 800, budget, single_line=True),
        # Legacy report events used a single `summary` field. Preserve it bounded so older logs and
        # finalization receipts remain readable while modern structured fields stay canonical.
        "summary": _text(src.get("summary", ""), 4_000, budget),
        "verdict": _text(_report_verdict(src.get("verdict", "")), 4_000, budget),
        "champion_summary": _text(src.get("champion_summary", ""), 4_000, budget),
    }
    # Caveats are trust-significant narrative. Give them the shared budget before positive/ordinary
    # lists so a saturated report cannot durably erase its own warnings.
    for field in _REPORT_LIST_FIELDS:
        out[field] = [_text(value, 1_200, budget, single_line=True)
                      for value in _items(src.get(field), 32)]
    out["at_node"] = (src.get("at_node") if type(src.get("at_node")) is int
                      and 0 <= src.get("at_node") <= (1 << 63) - 1 else None)
    out["trigger"] = _text(src.get("trigger", ""), 64, budget, single_line=True)
    return out
