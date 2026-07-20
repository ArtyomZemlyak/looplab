"""Bounded canonical forms for untrusted advisory sidecars.

Research memos and generated reports are audit/UI data, not replay authority, but malformed legacy
events still flow through replay and downstream cadence checks.  Normalize at both writer and replay
boundaries so an oversized or wrong-shaped sidecar cannot crash the engine or exhaust a renderer.
"""
from __future__ import annotations

import itertools
import math

from looplab.trust.redact import is_secret_key_name, redact_persisted_text
from looplab.trust.source_identity import canonical_source_ref, valid_source_identity


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
    if depth > 5:
        return "<depth-limited>"
    if isinstance(value, str):
        return _text(value, 2_000, budget)
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        return value if -(1 << 63) <= value <= (1 << 63) - 1 else _text(value, 128, budget)
    if type(value) is float:
        return value if math.isfinite(value) else _text(value, 32, budget)
    if isinstance(value, dict):
        out = {}
        for key, child in itertools.islice(value.items(), 64):
            if items[0] <= 0:
                break
            items[0] -= 1
            safe_key = _text(key, 128, budget, single_line=True)
            if is_secret_key_name(key):
                out[safe_key] = "***"
                budget[0] = max(0, budget[0] - 3)
            else:
                out[safe_key] = _tree(child, budget, items, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for child in itertools.islice(value, 64):
            if items[0] <= 0:
                break
            items[0] -= 1
            out.append(_tree(child, budget, items, depth + 1))
        return out
    return _text(value, 2_000, budget)


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
        "proposed_ideas": [],
        "at_node": (src.get("at_node") if type(src.get("at_node")) is int
                    and 0 <= src.get("at_node") <= (1 << 63) - 1 else None),
        "trigger": _text(src.get("trigger", ""), 64, budget, single_line=True),
    }
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
    out["recommended_directions"] = [
        _text(v, 1_200, budget, single_line=True)
        for v in _items(src.get("recommended_directions"), 16)
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
