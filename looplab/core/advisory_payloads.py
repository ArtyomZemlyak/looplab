"""Bounded canonical forms for untrusted advisory sidecars.

Research memos and generated reports are audit/UI data, not replay authority, but malformed legacy
events still flow through replay and downstream cadence checks.  Normalize at both writer and replay
boundaries so an oversized or wrong-shaped sidecar cannot crash the engine or exhaust a renderer.
"""
from __future__ import annotations

import itertools
import math

from looplab.trust.redact import is_secret_key_name, redact_persisted_text


MAX_RESEARCH_SOURCES = 64
_MAX_ADVISORY_TEXT = 64_000
_MAX_TREE_ITEMS = 512


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


def sanitize_research_memo_payload(payload) -> dict:
    """Canonicalize a model-, tool-, or legacy-event research memo."""
    src = payload if isinstance(payload, dict) else {}
    budget = [_MAX_ADVISORY_TEXT]
    tree_items = [_MAX_TREE_ITEMS]
    out = {
        "summary": _text(src.get("summary", ""), 4_000, budget),
        "reasoning": _text(src.get("reasoning", ""), 12_000, budget),
        "findings": [_text(v, 1_200, budget) for v in _items(src.get("findings"), 32)],
        "claims": [],
        "sources": [],
        "recommended_directions": [
            _text(v, 1_200, budget, single_line=True)
            for v in _items(src.get("recommended_directions"), 16)
        ],
        "proposed_ideas": [
            _tree(v, budget, tree_items) for v in _items(src.get("proposed_ideas"), 16)
        ],
        "at_node": (src.get("at_node") if type(src.get("at_node")) is int
                    and 0 <= src.get("at_node") <= (1 << 63) - 1 else None),
        "trigger": _text(src.get("trigger", ""), 64, budget, single_line=True),
    }
    for claim in _items(src.get("claims"), 64):
        if not isinstance(claim, dict):
            continue
        out["claims"].append({
            "statement": _text(claim.get("statement", ""), 1_600, budget),
            "node_ids": [n for n in _items(claim.get("node_ids"), 64)
                         if type(n) is int and 0 <= n <= (1 << 63) - 1],
            "urls": [_text(v, 1_600, budget, single_line=True)
                     for v in _items(claim.get("urls"), 16)],
        })
    for source in _items(src.get("sources"), MAX_RESEARCH_SOURCES):
        if not isinstance(source, dict):
            continue
        out["sources"].append({
            "title": _text(source.get("title", ""), 400, budget, single_line=True),
            "url": _text(source.get("url", ""), 1_600, budget, single_line=True),
            "snippet": _text(source.get("snippet", ""), 200, budget),
        })
    if "verification" in src:
        out["verification"] = _tree(src["verification"], budget, tree_items)
    return out


_REPORT_LIST_FIELDS = ("what_worked", "learnings", "what_didnt", "next_directions", "caveats")


def sanitize_report_payload(payload) -> dict:
    """Canonicalize a generated or legacy run-report payload."""
    src = payload if isinstance(payload, dict) else {}
    budget = [_MAX_ADVISORY_TEXT]
    out = {
        "headline": _text(src.get("headline", ""), 800, budget, single_line=True),
        # Legacy report events used a single `summary` field. Preserve it bounded so older logs and
        # finalization receipts remain readable while modern structured fields stay canonical.
        "summary": _text(src.get("summary", ""), 4_000, budget),
        "verdict": _text(src.get("verdict", ""), 4_000, budget),
        "champion_summary": _text(src.get("champion_summary", ""), 4_000, budget),
    }
    for field in _REPORT_LIST_FIELDS:
        out[field] = [_text(value, 1_200, budget, single_line=True)
                      for value in _items(src.get(field), 32)]
    out["at_node"] = (src.get("at_node") if type(src.get("at_node")) is int
                      and 0 <= src.get("at_node") <= (1 << 63) - 1 else None)
    out["trigger"] = _text(src.get("trigger", ""), 64, budget, single_line=True)
    return out
