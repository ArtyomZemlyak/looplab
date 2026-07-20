"""UI projection of the trace (ADR-17): join the research tree (from `events.jsonl` → RunState)
to its execution detail (from `spans.jsonl`). Pure reader of files-as-truth — never a source of
truth. Produces a per-node span forest the HTML view and the future React UI both consume.

Spans nest by (trace_id, span_id, parent_id); each top-level operation is its own trace tagged
with node_id (see tracing.py), so we group traces by node_id and build a child tree per trace.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict, deque
from itertools import islice
from typing import Optional

from looplab.core.models import RunState
from looplab.trust.redact import is_secret_key_name, redact_persisted_text


_MAX_SPAN_ID_CHARS = 256
_MAX_NODE_ID_CHARS = 128
_MAX_TRACE_TOKENS = (1 << 63) - 1
_MAX_TRACE_SECONDS = 1e15
_MAX_TRACE_FLOAT = sys.float_info.max
_MAX_PARENT_HOPS = 1024

# Public projection contract.  ``spans.jsonl`` is files-as-truth, but it is not a trusted API
# payload: custom exporters and hand-edited/corrupt runs can put arbitrary objects, credentials and
# multi-megabyte strings in it.  Every browser-facing/indexed shape passes through this versioned,
# bounded allowlist.  Bump together with span_index._SCHEMA when the record shape changes.
TRACE_PROJECTION_SCHEMA = 2
TRACE_VIEW_SPAN_CAP = 1024
TRACE_NODE_SPAN_CAP = 512
# Ceiling for the UI's "load more spans" control on a single node's trace: the default cap stays 512
# (fast expand), but a user can page a heavily-repaired node up to this bound on demand. Still O(node)
# — a bigger cap only surfaces more of THAT node's already-scoped spans (see appstate.node_trace_view).
TRACE_NODE_SPAN_CAP_MAX = 4096
TRACE_DETAIL_SPAN_CAP = 256
TRACE_CONVERSATION_SPAN_CAP = 512

_SPAN_TEXT_BUDGET = 8192
_META_TEXT_CAP = 256
_EVENTS_CAP = 16
_EVENT_FIELDS_CAP = 8
_STRUCT_ITEMS_CAP = 32
_STRUCT_DEPTH_CAP = 3
_TOOL_CALLS_CAP = 16
_CONVERSATION_STAGE_CAP = 64
_CONVERSATION_TURN_CAP = 256


def unavailable_projection(*, light: bool | None = None) -> dict:
    """Projection receipt for a source that could not be read at all."""
    # CODEX AGENT: unavailable cardinality is unknown. Never turn an I/O failure into plausible zero
    # counts (or ``truncated=False``), because clients would present missing telemetry as complete.
    projection = {
        "schema": TRACE_PROJECTION_SCHEMA,
        "unavailable": True,
        "truncated": True,
    }
    if light is not None:
        projection["light"] = bool(light)
    return projection

_SPAN_FIELDS = {
    "name", "kind", "trace_id", "span_id", "parent_id", "run_id", "status",
    "start", "end", "duration_s", "attributes", "events", "_projection",
}
_ATTRIBUTE_FIELDS = {
    # topology / conversation reconstruction
    "node_id", "phase", "phase_span", "input_from", "input_carry", "input_partial",
    # generation / tool observation
    "model", "op", "model_parameters", "tool", "tool_calls", "input", "output",
    "thinking", "usage", "cost", "level",
    # engine/evaluation operation diagnostics used by the Inspector
    "stage", "exit_code", "timed_out", "reused", "sandboxed", "seed", "blocks",
    "attempt", "reason", "package", "trigger", "operator", "parent_id", "proxy_score",
    "proxy_skipped", "eval_seconds", "metric", "ok", "repair_attempts", "violations",
    "drift", "error_reason", "feasible", "robust_metric", "materialized",
    "handoff_from", "handoff_to",
}
_ATTR_TEXT_FIELDS = {
    "phase", "model", "op", "tool", "level", "stage", "reason", "package", "trigger",
    "operator", "error_reason", "materialized", "handoff_from", "handoff_to",
}
_ATTR_BOOL_FIELDS = {
    "input_partial", "timed_out", "reused", "sandboxed", "proxy_skipped", "ok", "drift",
    "feasible",
}
_ATTR_INT_FIELDS = {
    "input_carry", "exit_code", "seed", "blocks", "attempt", "repair_attempts", "violations",
}
_ATTR_FLOAT_FIELDS = {"proxy_score", "eval_seconds", "metric", "robust_metric"}
_EVENT_FIELDS = {"error", "type", "message", "n", "count", "status", "stage", "step", "reason"}


def _projection_counter(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= (1 << 31) - 1 else 0


class _ProjectionBudget:
    """Shared per-span text budget plus honest, idempotent omission accounting."""

    def __init__(self, previous=None):
        previous = previous if isinstance(previous, dict) else {}
        self.remaining = _SPAN_TEXT_BUDGET
        self.counts = {key: _projection_counter(previous.get(key)) for key in (
            "omitted_fields", "omitted_attributes", "omitted_events", "omitted_messages",
            "omitted_tool_calls", "omitted_items", "omitted_chars",
        )}
        self.previous_truncated = previous.get("truncated") is True

    def omit(self, key: str, n: int = 1) -> None:
        self.counts[key] = min((1 << 31) - 1, self.counts.get(key, 0) + max(0, int(n)))

    def text(self, value, *, cap: int = _META_TEXT_CAP, single_line: bool = False) -> str:
        allowed = min(max(0, int(cap)), self.remaining)
        raw = redact_persisted_text(
            value, max_chars=max(allowed, 0), entropy=True, single_line=single_line)
        # ``redact_persisted_text`` deliberately does not expose the secret's original length.  Count
        # only known input truncation; the marker still makes the truncation visible to the reader.
        try:
            original_len = len(str(value)) if value is not None else 0
        except Exception:  # noqa: BLE001 - opaque diagnostics are projected as unavailable text
            original_len = 0
        if original_len > allowed:
            self.omit("omitted_chars", original_len - allowed)
        self.remaining = max(0, self.remaining - len(raw))
        return raw

    def metadata(self) -> dict:
        counts = {key: value for key, value in self.counts.items() if value}
        truncated = self.previous_truncated or bool(counts) or self.remaining <= 0
        return {"schema": TRACE_PROJECTION_SCHEMA, "truncated": truncated, **counts}


def _safe_structured(value, budget: _ProjectionBudget, *, depth: int = 0):
    """Small JSON-compatible structured value with secret-key masking and shared text accounting."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return budget.text(value, cap=2000)
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -(1 << 63) <= value <= (1 << 63) - 1 else budget.text(value, cap=64)
    if isinstance(value, float):
        return value if math.isfinite(value) else budget.text(value, cap=32, single_line=True)
    if depth >= _STRUCT_DEPTH_CAP:
        budget.omit("omitted_items")
        return "<depth-limited>"
    if isinstance(value, dict):
        out = {}
        items = list(islice(value.items(), _STRUCT_ITEMS_CAP + 1))
        if len(items) > _STRUCT_ITEMS_CAP:
            budget.omit("omitted_items", max(1, len(value) - _STRUCT_ITEMS_CAP))
        for raw_key, child in items[:_STRUCT_ITEMS_CAP]:
            key = budget.text(raw_key, cap=80, single_line=True)
            if not key:
                budget.omit("omitted_items")
                continue
            if is_secret_key_name(raw_key):
                out[key] = "***"
            else:
                out[key] = _safe_structured(child, budget, depth=depth + 1)
            if budget.remaining <= 0:
                break
        return out
    if isinstance(value, (list, tuple)):
        items = list(islice(value, _STRUCT_ITEMS_CAP + 1))
        if len(items) > _STRUCT_ITEMS_CAP:
            try:
                omitted = len(value) - _STRUCT_ITEMS_CAP
            except Exception:  # noqa: BLE001
                omitted = 1
            budget.omit("omitted_items", max(1, omitted))
        return [_safe_structured(item, budget, depth=depth + 1)
                for item in items[:_STRUCT_ITEMS_CAP] if budget.remaining > 0]
    return budget.text(value, cap=256)


def _project_messages(value, budget: _ProjectionBudget) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        if value is not None:
            budget.omit("omitted_messages")
        return []
    total = len(value)
    kept = list(value[:_MSGS_CAP])
    if total > _MSGS_CAP:
        head = _MSGS_CAP // 2
        kept = list(value[:head]) + list(value[-(_MSGS_CAP - head):])
        budget.omit("omitted_messages", total - _MSGS_CAP)
    out = []
    for raw in kept:
        if not isinstance(raw, dict):
            budget.omit("omitted_messages")
            continue
        out.append({
            "role": budget.text(raw.get("role", "user"), cap=32, single_line=True) or "user",
            "content": budget.text(raw.get("content", ""), cap=_IO_CAP),
        })
        if budget.remaining <= 0:
            budget.omit("omitted_messages", max(0, len(kept) - len(out)))
            break
    return out


def _project_tool_calls(value, budget: _ProjectionBudget) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        if value is not None:
            budget.omit("omitted_tool_calls")
        return []
    calls = list(value[:_TOOL_CALLS_CAP])
    if len(value) > _TOOL_CALLS_CAP:
        budget.omit("omitted_tool_calls", len(value) - _TOOL_CALLS_CAP)
    out = []
    for raw in calls:
        if not isinstance(raw, dict):
            budget.omit("omitted_tool_calls")
            continue
        out.append({
            "name": budget.text(raw.get("name", ""), cap=128, single_line=True),
            "arguments": budget.text(raw.get("arguments", ""), cap=1000),
        })
    return out


def _project_events(value, budget: _ProjectionBudget) -> list[dict]:
    if not isinstance(value, list):
        if value is not None:
            budget.omit("omitted_events")
        return []
    selected = value[:_EVENTS_CAP]
    if len(value) > _EVENTS_CAP:
        budget.omit("omitted_events", len(value) - _EVENTS_CAP)
    out = []
    for raw in selected:
        if not isinstance(raw, dict):
            budget.omit("omitted_events")
            continue
        event = {"name": budget.text(raw.get("name", "event"), cap=80, single_line=True) or "event"}
        allowed = [(key, child) for key, child in raw.items() if key != "name" and key in _EVENT_FIELDS]
        budget.omit("omitted_fields", sum(1 for key in raw if key != "name" and key not in _EVENT_FIELDS))
        if len(allowed) > _EVENT_FIELDS_CAP:
            budget.omit("omitted_fields", len(allowed) - _EVENT_FIELDS_CAP)
        for key, child in allowed[:_EVENT_FIELDS_CAP]:
            if key in {"n", "count"}:
                event[key] = _safe_token_count(child)
            else:
                event[key] = budget.text(child, cap=500 if key in {"error", "message", "reason"} else 160,
                                         single_line=key not in {"error", "message", "reason"})
        out.append(event)
    return out


def _normalized_id(value) -> Optional[str]:
    """Return one bounded, hashable span/trace id, or ``None`` when it is unusable.

    Current writers emit compact hex strings. Bounded non-negative integers are accepted for old/custom
    exporters and canonicalized to strings so parent references still compare consistently. Containers,
    booleans and enormous strings are invalid rather than becoming dictionary keys in every trace view.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value or len(value) > _MAX_SPAN_ID_CHARS:
            return None
        # IDs are echoed in routes, trees and persisted indexes.  A custom exporter must not be able
        # to smuggle a credential/control payload through the identity plane, which intentionally does
        # not otherwise redact values (redacting an ID would silently change topology).  Quarantine the
        # observation instead; ordinary hex/UUID and legacy compact IDs remain byte-identical.
        safe = redact_persisted_text(
            value, max_chars=_MAX_SPAN_ID_CHARS, entropy=True, single_line=True)
        return value if safe == value else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_TRACE_TOKENS:
        return str(value)
    return None


def _finite_number(value, *, default=0.0, nonnegative: bool = False,
                   maximum: float = _MAX_TRACE_FLOAT):
    """Coerce an untrusted JSON scalar without allowing NaN/inf/huge values into sorting or sums."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and len(value.strip()) > 64:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if (not math.isfinite(number) or abs(number) > maximum
            or (nonnegative and number < 0.0)):
        return default
    if isinstance(value, (int, float)):
        return value
    return number


def _safe_token_count(value) -> int:
    """Signed-int64, non-negative token projection shared by normalization and roll-ups."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_TRACE_TOKENS else 0
    if isinstance(value, str) and len(value.strip()) > 32:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or not number.is_integer():
        return 0
    result = int(number)
    return result if 0 <= result <= _MAX_TRACE_TOKENS else 0


def _normalize_span(value) -> Optional[dict]:
    """Project one durable span through the strict browser/index security contract.

    A complete, valid-JSON line with a bad schema is a quarantined observation, not an end-of-log marker.
    Invalid required ids drop the one line; recoverable fields degrade to safe defaults.  Unknown keys,
    raw exception payloads and unbounded structured values stay in ``spans.jsonl`` but never cross into
    an index or response.  Omission metadata is carried on the span and remains idempotent if a persisted
    index record is normalized again after restart.
    """
    if not isinstance(value, dict):
        return None
    span_id = _normalized_id(value.get("span_id"))
    trace_id = _normalized_id(value.get("trace_id"))
    if span_id is None or trace_id is None:
        return None

    budget = _ProjectionBudget(value.get("_projection"))
    budget.omit("omitted_fields", sum(1 for key in value if key not in _SPAN_FIELDS))
    parent_id = _normalized_id(value.get("parent_id"))
    if value.get("parent_id") is not None and parent_id is None:
        budget.omit("omitted_fields")
    span = {
        "name": budget.text(value.get("name", "span"), cap=160, single_line=True) or "span",
        "kind": budget.text(value.get("kind", "operation"), cap=32, single_line=True) or "operation",
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": parent_id,
        "run_id": budget.text(value.get("run_id", ""), cap=256, single_line=True),
        "status": budget.text(value.get("status", ""), cap=32, single_line=True),
        "start": _finite_number(value.get("start", 0.0), maximum=_MAX_TRACE_SECONDS),
    }
    if "end" in value:
        span["end"] = _finite_number(value.get("end"), maximum=_MAX_TRACE_SECONDS)
    if "duration_s" in value:
        span["duration_s"] = _finite_number(
            value.get("duration_s"), nonnegative=True, maximum=_MAX_TRACE_SECONDS)

    raw_attributes = value.get("attributes")
    raw_attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    budget.omit("omitted_attributes", sum(1 for key in raw_attributes if key not in _ATTRIBUTE_FIELDS))
    attributes = {}
    node_id = raw_attributes.get("node_id")
    if isinstance(node_id, int) and not isinstance(node_id, bool) and 0 <= node_id <= _MAX_TRACE_TOKENS:
        attributes["node_id"] = node_id
    elif isinstance(node_id, str) and node_id and len(node_id) <= _MAX_NODE_ID_CHARS:
        attributes["node_id"] = budget.text(node_id, cap=_MAX_NODE_ID_CHARS, single_line=True)
    elif "node_id" in raw_attributes:
        budget.omit("omitted_attributes")
    for key in ("phase_span", "input_from"):
        if key in raw_attributes:
            normalized = _normalized_id(raw_attributes.get(key))
            if normalized is None:
                budget.omit("omitted_attributes")
            else:
                attributes[key] = normalized
    # CODEX AGENT: _ATTR_TEXT_FIELDS / _ATTR_BOOL_FIELDS / _ATTR_INT_FIELDS / _ATTR_FLOAT_FIELDS
    # are SETS, so these four loops insert into `attributes` in string-hash order — randomized per
    # process via PYTHONHASHSEED. The serialized projection (and any persisted index record built from
    # it) is therefore not byte-stable across two server runs, the same raw-set-iteration defect this
    # change series fixed in project_hierarchy/project_lens/concept_graph. Iterate sorted(...) (or
    # make the field groups tuples) so identical spans serialize identically everywhere.
    for key in sorted(_ATTR_TEXT_FIELDS):
        if key in raw_attributes:
            attributes[key] = budget.text(
                raw_attributes.get(key), cap=512 if key in {"reason", "error_reason", "materialized"} else 160,
                single_line=key not in {"reason", "error_reason"})
    for key in sorted(_ATTR_BOOL_FIELDS):
        if key in raw_attributes:
            if isinstance(raw_attributes.get(key), bool):
                attributes[key] = raw_attributes[key]
            else:
                budget.omit("omitted_attributes")
    for key in sorted(_ATTR_INT_FIELDS):
        if key in raw_attributes:
            item = raw_attributes.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and -(1 << 63) <= item <= (1 << 63) - 1:
                attributes[key] = item
            else:
                budget.omit("omitted_attributes")
    for key in sorted(_ATTR_FLOAT_FIELDS):
        if key in raw_attributes:
            attributes[key] = _finite_number(raw_attributes.get(key))
    if "parent_id" in raw_attributes:
        parent_node = raw_attributes.get("parent_id")
        if isinstance(parent_node, int) and not isinstance(parent_node, bool) and 0 <= parent_node <= _MAX_TRACE_TOKENS:
            attributes["parent_id"] = parent_node
        else:
            budget.omit("omitted_attributes")

    usage = raw_attributes.get("usage")
    usage = dict(usage) if isinstance(usage, dict) else {}
    safe_usage = {}
    for key in ("prompt", "completion", "total", "context"):
        if key in usage:
            safe_usage[key] = _safe_token_count(usage[key])
    if "usage" in raw_attributes:
        attributes["usage"] = safe_usage
        if not isinstance(raw_attributes.get("usage"), dict):
            budget.omit("omitted_attributes")
        elif len(usage) > len(safe_usage):
            budget.omit("omitted_items", len(usage) - len(safe_usage))
    if "cost" in raw_attributes:
        attributes["cost"] = _finite_number(raw_attributes.get("cost"), nonnegative=True)
    if "model_parameters" in raw_attributes:
        attributes["model_parameters"] = _safe_structured(raw_attributes.get("model_parameters"), budget)
    if "tool_calls" in raw_attributes:
        attributes["tool_calls"] = _project_tool_calls(raw_attributes.get("tool_calls"), budget)
    if "input" in raw_attributes:
        attributes["input"] = (_project_messages(raw_attributes.get("input"), budget)
                               if span["kind"] == "generation"
                               else _safe_structured(raw_attributes.get("input"), budget))
    for key in ("output", "thinking"):
        if key in raw_attributes:
            item = raw_attributes.get(key)
            attributes[key] = (budget.text(item, cap=_IO_CAP)
                               if isinstance(item, str) or item is None
                               else _safe_structured(item, budget))
    if span["kind"] == "generation" and budget.counts.get("omitted_messages"):
        attributes["input_partial"] = True
    span["attributes"] = attributes
    span["events"] = _project_events(value.get("events"), budget)
    span["_projection"] = budget.metadata()
    return span


def _normalize_spans(spans) -> list[dict]:
    out: list[dict] = []
    for value in spans or ():
        normalized = _normalize_span(value)
        if normalized is not None:
            out.append(normalized)
    return out


def _bounded_tail(values, cap: int) -> tuple[list, int]:
    """Return at most cap newest values plus the exact number observed, without a full copy."""
    cap = max(0, int(cap))
    if isinstance(values, (list, tuple)):
        return list(values[-cap:]) if cap else [], len(values)
    tail = deque(maxlen=cap)
    total = 0
    for value in values or ():
        total += 1
        if cap:
            tail.append(value)
    return list(tail), total


def _bounded_node_trace_tail(values, node_id, cap: int) -> tuple[list, int]:
    """Cap a node conversation only after selecting the traces attributed to that node.

    The no-index path receives the entire run, whereas the indexed path already receives only the
    target node's traces.  Taking the whole-run tail first lets sufficiently busy, unrelated nodes
    evict an older target node completely and also makes the two paths report different totals.
    ``build_conversation`` is public but its normal inputs are concrete snapshots (``load_spans`` or
    ``SpanIndex.full_spans_for_node``); retain a bounded one-pass degradation for exotic iterables.
    """
    if not isinstance(values, (list, tuple)):
        return _bounded_tail(values, cap)

    target = str(node_id)
    matching_trace_ids: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        trace_id = _normalized_id(raw.get("trace_id"))
        raw_node_id = _node_id_of(raw)
        if trace_id is not None and raw_node_id is not None and str(raw_node_id) == target:
            matching_trace_ids.add(trace_id)

    # CODEX AGENT: Filtering precedes the global cap.  This must stay equivalent to the index's
    # ``node_tids -> rows -> tail`` path or an unrelated busy node can erase the requested story.
    matching = (
        raw for raw in values
        if isinstance(raw, dict) and _normalized_id(raw.get("trace_id")) in matching_trace_ids
    )
    return _bounded_tail(matching, cap)


def _response_projection(*, total_spans: int, visible_spans: int, light: bool = False,
                         truncated_spans: int = 0, **extra) -> dict:
    total = max(visible_spans, _projection_counter(total_spans))
    omitted = max(0, total - visible_spans)
    clean_extra = {key: _projection_counter(value) for key, value in extra.items()}
    truncated = omitted > 0 or truncated_spans > 0 or any(
        value > 0 for key, value in clean_extra.items() if key.startswith("omitted_"))
    return {
        "schema": TRACE_PROJECTION_SCHEMA,
        "light": bool(light),
        "truncated": truncated,
        "total_spans": total,
        "visible_spans": visible_spans,
        "omitted_spans": omitted,
        "truncated_spans": max(0, truncated_spans),
        **clean_extra,
    }


def load_spans(path: str | os.PathLike) -> list[dict]:
    """Read spans.jsonl, quarantining complete valid-JSON rows with an invalid span shape."""
    from looplab.events.eventstore import iter_jsonl
    try:
        # `iter_jsonl` intentionally treats a missing append-only log as empty, but Path.exists() also
        # suppresses permission/stat errors. Preflight the trace source so those errors reach the route's
        # explicit unavailable envelope instead of masquerading as a successful empty projection.
        with open(path, "rb"):
            pass
    except FileNotFoundError:
        return []
    return _normalize_spans(iter_jsonl(path))


def _tree(spans: list[dict], *, _normalized: bool = False) -> list[dict]:
    """Build the parent->child forest for one trace from a flat span list."""
    if not _normalized:
        spans = _normalize_spans(spans)
    by_id = {s["span_id"]: {**s, "children": []} for s in spans}
    roots = []
    for s in by_id.values():
        parent = by_id.get(s.get("parent_id"))
        (parent["children"] if parent else roots).append(s)
    # deterministic order: by start time. Iterative (explicit stack), NOT recursive: a pathologically
    # deep parent_id chain in a crafted/corrupt spans.jsonl would otherwise blow Python's recursion limit
    # and crash the view — the exact "tolerate corrupt spans" contract the projections harden for (and why
    # hydrate_inputs is already iterative). Each `children` list is sorted independently, so order is
    # identical to the recursive version.
    stack = [roots]
    while stack:
        level = stack.pop()
        level.sort(key=lambda n: n.get("start", 0.0))
        for n in level:
            if n["children"]:
                stack.append(n["children"])
    return roots


def _node_id_of(span: dict) -> Optional[int | str]:
    attributes = span.get("attributes")
    return attributes.get("node_id") if isinstance(attributes, dict) else None


def _rollup(spans: list[dict]) -> dict:
    """Aggregate generation/tool usage over a flat span list — the Langfuse-style trace totals
    (tokens + cost summed from every generation, plus observation counts). Returned per node and
    for the whole run so the UI can show 'N calls · K tok · $C' without re-summing the tree."""
    gens = [s for s in spans if s.get("kind") == "generation"]
    tools = [s for s in spans if s.get("kind") == "tool"]
    pt = ct = tt = 0
    peak = 0
    cost = 0.0
    for g in gens:
        raw_attributes = g.get("attributes")
        a = raw_attributes if isinstance(raw_attributes, dict) else {}
        raw_usage = a.get("usage")
        u = raw_usage if isinstance(raw_usage, dict) else {}
        p = _safe_token_count(u.get("prompt"))
        pt = min(_MAX_TRACE_TOKENS, pt + p)            # SUM of every call's prompt (billed — a tool loop
        peak = max(peak, p)                           # re-sends the growing context, so this is O(turns²))
        ct = min(_MAX_TRACE_TOKENS, ct + _safe_token_count(u.get("completion")))
        tt = min(_MAX_TRACE_TOKENS, tt + _safe_token_count(u.get("total")))
        item_cost = _finite_number(a.get("cost"), nonnegative=True)
        cost = min(_MAX_TRACE_FLOAT, cost + item_cost)
    # `context` = the LARGEST single prompt = how big the LLM's context window actually got (what the
    # user reads as "the context"), distinct from `total`/`prompt` which SUM the same context re-sent
    # every turn (billed cost, not context size). The UI shows context↑ + output↓, billed in the tooltip.
    return {"generations": len(gens), "tools": len(tools),
            "tokens": {"prompt": pt, "completion": ct, "total": tt, "context": peak},
            "cost": round(cost, 6)}


# Trace-view I/O caps. A real repo-developer generation carries a 100KB+ prompt, and a long run
# accumulates hundreds of them — the recorded trace of one such run was ~52 MB, which crashes the
# browser (observed: a black screen). The browser VIEW applies an additional bounded/redacted
# projection to the already capture-filtered record, including a head/tail message selection.
_IO_CAP = 2000          # max chars per single message/output/reasoning string
_MSGS_CAP = 10          # max messages kept in a generation's `input` (head + tail)


def _cap_str(v, n: int = _IO_CAP):
    if not isinstance(v, str):
        return v
    return redact_persisted_text(v, max_chars=max(0, int(n)), entropy=True)


def _cap_msgs(msgs: list) -> list:
    if not isinstance(msgs, list):
        return msgs
    return _project_messages(msgs, _ProjectionBudget())


def _cap_span_io(s: dict) -> dict:
    """Return one already-normalized span with bounded/redacted I/O and updated omission truth."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in ("input", "output", "thinking")):
        return s
    budget = _ProjectionBudget(s.get("_projection"))
    a = dict(a)
    for key in ("output", "thinking"):
        if key in a:
            a[key] = (budget.text(a.get(key), cap=_IO_CAP)
                      if isinstance(a.get(key), str) or a.get(key) is None
                      else _safe_structured(a.get(key), budget))
    if "input" in a:
        a["input"] = (_project_messages(a.get("input"), budget)
                      if s.get("kind") == "generation"
                      else _safe_structured(a.get("input"), budget))
    if s.get("kind") == "generation" and budget.counts.get("omitted_messages"):
        a["input_partial"] = True
    return {**s, "attributes": a, "_projection": budget.metadata()}


_STRIP_IO_KEYS = ("input", "output", "thinking", "input_carry", "input_from",
                  "model_parameters", "tool_calls")


def _strip_span_io(s: dict) -> dict:
    """Drop the heavy I/O entirely — the run-level trace (Dock timeline) needs only structure, timing,
    model + token usage, not the prompts/outputs. Keeps the whole-run payload tiny; detail endpoints
    serve a bounded/redacted diagnostic projection for the Inspector. Also drops the delta bookkeeping
    (`input_carry`/`input_from`): with no `input` they carry no meaning, and leaving them would let a
    stray `hydrate_inputs` on a light span reconstruct to `[]` (its `input` is gone)."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in _STRIP_IO_KEYS):
        return s
    return {**s, "attributes": {k: v for k, v in a.items() if k not in _STRIP_IO_KEYS}}


# ── linear conversation projection ───────────────────────────────────────────────────────────────
# The recorded span tree can retain a message-list projection for every generation — but the agent
# tool-loop re-sends the whole conversation on every turn, so successive generations duplicate the
# system+user prompt and every prior turn (a 206-generation node re-sends the history 206×). The
# conversation projection reconstructs the loop as a linear, de-duplicated thread: the system+user
# REQUEST once per sub-loop, then each generation's DELTA (reasoning + text + which tools it called)
# interleaved with the tool executions. It reads like the agent's actual train of thought.

def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _iter_parent_spans(span: dict, by_id: dict, *, stop_id=None):
    """Yield a bounded, cycle-safe parent chain, excluding ``stop_id`` when supplied."""
    current_id = span.get("span_id")
    seen = {current_id} if isinstance(current_id, (str, int)) else set()
    parent_id = span.get("parent_id")
    for _ in range(_MAX_PARENT_HOPS):
        if parent_id is None or parent_id == stop_id:
            return
        try:
            if parent_id in seen:
                return
            seen.add(parent_id)
            parent = by_id.get(parent_id)
        except TypeError:
            return
        if not isinstance(parent, dict):
            return
        yield parent
        parent_id = parent.get("parent_id")


def _seg_label(gen_span: dict, by_id: dict) -> Optional[str]:
    """The sub-loop a generation belongs to (propose / implement / repair / grade), to label its request
    boundary with its phase. Prefer the `phase` stamped on the span itself (tracing._phase_ctx) — correct
    even LIVE, before the parent operation span is flushed to disk; fall back to walking to the nearest
    ancestor operation span for older traces written before phase-stamping."""
    ph = (gen_span.get("attributes") or {}).get("phase")
    if ph:
        return ph
    for cur in _iter_parent_spans(gen_span, by_id):
        if cur.get("kind") in (None, "operation"):
            return cur.get("name")
    return None


def _thread_turns(spans_sorted: list[dict], by_id: dict) -> list[dict]:
    """Walk one trace's spans in time order → linear turns. A `request` is emitted at the first
    generation and again whenever the sent message count DROPS (the context reset that marks a new
    sub-loop — a fresh system+user), so the request is shown once per sub-loop, never re-duplicated.
    Every generation contributes only its delta (thinking + output + tool_calls); tools interleave."""
    turns: list[dict] = []
    prev_in: Optional[int] = None
    for s in spans_sorted:
        kind = s.get("kind")
        a = s.get("attributes") or {}
        if s.get("name") == "stage_started":
            continue          # a zero-work live-band anchor (command_eval) — not a real turn to show
        if kind == "generation":
            inp = a.get("input") if isinstance(a.get("input"), list) else []
            n = len(inp)
            # A `request` marks a sub-loop start. Delta-encoded logs (tracing.generation) say so
            # explicitly: a base generation stores `input_carry == 0` (it carried NOTHING from a prior
            # generation), so its `input` is the retained initial-context projection and the request is
            # shown from it; a non-base generation carries a real prefix (carry > 0) and its stored `input` is
            # only the delta — (correctly) not a boundary. Keying on `input_carry == 0` (not
            # `input_from is None`) matches `hydrate_inputs`, which likewise treats carry=0 as
            # self-contained, so a degenerate carry=0-with-back-ref span is still read as a base.
            # Old full logs have no `input_carry` → fall back to the message-count-drop heuristic.
            is_base = (a.get("input_carry") == 0) if ("input_carry" in a) \
                else (prev_in is None or n <= prev_in)
            if is_base:                              # first call / context reset → new sub-loop
                turns.append({"type": "request", "label": _seg_label(s, by_id),
                              "messages": [{"role": m.get("role", "user"),
                                            "content": _cap_str(_as_text(m.get("content")))}
                                           for m in inp if isinstance(m, dict)]})
            prev_in = n
            out = a.get("output")
            think = a.get("thinking")
            turns.append({"type": "generation",
                          "think": _cap_str(think) if isinstance(think, str) and think else None,
                          "output": _cap_str(out if isinstance(out, str) else _as_text(out)),
                          "model": a.get("model"),
                          "tool_calls": [(tc.get("name") if isinstance(tc, dict) else tc)
                                         for tc in (a.get("tool_calls") or [])],
                          "usage": a.get("usage") or {},
                          "status": s.get("status"), "seconds": s.get("duration_s")})
        elif kind == "tool":
            turns.append({"type": "tool", "name": a.get("tool") or s.get("name") or "tool",
                          "input": _cap_str(_as_text(a.get("input"))),
                          "output": _cap_str(_as_text(a.get("output"))),
                          "status": s.get("status"), "seconds": s.get("duration_s")})
        elif kind == "operation" and a.get("stage"):
            # An eval PIPELINE stage (train / score — command_eval opens one op span per stage):
            # no LLM turns inside, but the reader wants it as a block in the node's life story
            # ("… Developer · implement, Train, Evaluate …"). Rendered via the tool turn shape.
            rc, to = a.get("exit_code"), a.get("timed_out")
            # `not rc` was truthy for BOTH exit 0 and a MISSING code — a stage span closed by an
            # exception before its exit_code was recorded (status "ERROR", rc None) then rendered "ok".
            if a.get("reused"):
                status = "reused"        # skipped on a repair re-eval; its earlier artifact is kept
            elif to:
                status = "timeout"
            elif rc is None:
                status = "error" if s.get("status") == "ERROR" else "?"
            else:
                status = "ok" if rc == 0 else f"exit {rc}"
            secs = s.get("duration_s")
            turns.append({"type": "tool", "name": str(a.get("stage")),
                          "input": "",
                          "output": f"{status}" + (f" · {round(float(secs), 1)}s" if secs else ""),
                          "status": s.get("status"), "seconds": secs})
        # other operation spans carry structure only — skipped in the linear reading view.
    return turns


def hydrate_inputs(spans: list[dict]) -> list[dict]:
    """Reconstruct the complete retained `input` of every delta-encoded generation in `spans` from its
    `input_from` chain (see `tracing.generation`): full = reconstruct(input_from)[:input_carry] + delta.
    Returns spans with `input` expanded and the `input_carry`/`input_from` bookkeeping dropped, so a
    reader (the single-observation view, the per-op trace tree) sees the complete diagnostic projection
    retained by tracing. Capture-time redaction and projection caps still apply, so this must not be
    described as the verbatim provider prompt. A generation with no `input_carry` (old full logs, or a
    fresh base) passes through unchanged. Reconstruct within the passed set (a whole trace) — the chain
    never leaves its trace.
    If an ANCESTOR span is absent (a torn/offset-skipped line — `span_index._read_full` drops one) the
    chain can't bottom out at its real base, so the reconstruction is a TRUNCATED prefix; such spans are
    stamped `input_partial=True` so a reader never presents a short input as a complete retained
    prompt projection."""
    spans = _normalize_spans(spans)
    by_sid = {s.get("span_id"): s for s in spans if s.get("span_id")}
    memo: dict = {}
    partial: dict = {}                    # sid -> True when its chain bottomed out at a missing ref/cycle

    def _full(sid) -> list:
        # Reconstruct iteratively (NOT recursively): a tool-loop can chain thousands of generations in
        # one sub-loop, and recursion would blow the stack (RecursionError past ~1000) on a deep chain
        # walked in non-file order. Walk UP the linear input_from chain collecting each delta, stopping
        # at a base / already-memoized ancestor / missing ref / cycle, then apply the deltas back DOWN.
        if sid in memo:
            return memo[sid]
        chain: list[tuple] = []           # (span_id, carry, delta_input), from `sid` upward
        seen: set = set()
        cur_sid = sid
        base: list = []
        broke = False                     # True ⇒ a referenced ancestor was missing → prefix is truncated
        while True:
            if cur_sid in memo:
                base = memo[cur_sid]
                broke = partial.get(cur_sid, False)
                break
            if cur_sid is None or cur_sid in seen:    # missing ref / cycle → empty base, INCOMPLETE
                base = []
                broke = True
                break
            seen.add(cur_sid)
            s = by_sid.get(cur_sid)
            if s is None:                             # referenced ancestor absent from the span set
                base = []
                broke = True
                break
            a = s.get("attributes") or {}
            cur = a.get("input")
            if "input_carry" not in a or not isinstance(cur, list):
                base = cur if isinstance(cur, list) else []
                break   # old log / non-list → input IS full
            frm = a.get("input_from")
            if frm is None:                            # self-contained base: its `input` is the full ctx
                memo[cur_sid] = list(cur)
                partial[cur_sid] = False
                base = memo[cur_sid]
                break
            # Coerce carry to a NON-NEGATIVE int: a malformed span (bit-rot on a network mount, or a
            # hand-edited log) whose input_carry is a string/float would make `full[:carry]` raise
            # TypeError and abort the WHOLE trace, and a negative carry would silently truncate the
            # prefix. Fall back to 0 (the delta stands as the full input) — the safe degradation the
            # non-list/absent-carry branch above already uses — instead of crashing the projection.
            raw_carry = a.get("input_carry")
            carry = raw_carry if (isinstance(raw_carry, int) and not isinstance(raw_carry, bool)
                                  and raw_carry >= 0) else 0
            chain.append((cur_sid, carry, cur))
            cur_sid = frm
        full = base
        for csid, carry, delta in reversed(chain):     # apply deltas base→leaf, memoizing every level
            full = list(full[:carry]) + list(delta)
            memo[csid] = full
            partial[csid] = broke
        if sid not in memo:
            memo[sid] = full
            partial[sid] = broke
        return memo[sid]

    out: list[dict] = []
    for s in spans:
        a = s.get("attributes")
        if isinstance(a, dict) and "input_carry" in a and s.get("kind") == "generation":
            na = {k: v for k, v in a.items() if k not in ("input_carry", "input_from")}
            na["input"] = _full(s.get("span_id"))
            if partial.get(s.get("span_id")):
                na["input_partial"] = True             # an ancestor was missing → `input` is truncated
            out.append({**s, "attributes": na})
        else:
            out.append(s)
    return out


def build_conversation(state: RunState, spans: list[dict], node_id, *, total_spans=None) -> dict:
    """Per-node linear conversation (companion to `build_trace_view`). One `stage` per trace tagged
    with this node (create_node / evaluate / …), each a de-duplicated thread of turns. Reader of
    files-as-truth; caps every string for the browser, but never re-sends the growing history."""
    selected, _observed_total = _bounded_node_trace_tail(
        spans, node_id, TRACE_CONVERSATION_SPAN_CAP)
    spans = _normalize_spans(selected)
    by_id = {s["span_id"]: s for s in spans}
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)
    stages: list[dict] = []
    matching_span_count = 0
    matching_spans: list[dict] = []
    for tid, ss in by_trace.items():
        ss_sorted = sorted(ss, key=lambda x: x.get("start", 0.0))
        # The REAL root may be absent LIVE: an operation span is written only on CLOSE, and
        # `create_node` closes at node END — so for the whole life of a node its trace has no root on
        # disk. The old code then fell back to the first span (a generation), missed the create_node
        # split branch entirely, and rendered the ENTIRE node as ONE flat band labeled "generation"
        # whose turns kept appending across role changes (the "Developer writes into the previous
        # Researcher block" bug). Grouping below never needs the root, only its ABSENCE handled.
        root = next((s for s in ss_sorted if s.get("parent_id") is None), None)
        first = root or (ss_sorted[0] if ss_sorted else None)
        nid = _node_id_of(first) if first else None
        if nid is None or str(nid) != str(node_id):
            continue
        matching_span_count += len(ss)
        matching_spans.extend(ss)
        # Split EVERY trace into its sub-loop bands (propose / stages / plan / implement / repair /
        # inline_repair / …) so the conversation reads as ordered role blocks. Wrapper roots
        # (`create_node` = "Author node", `seed_workspace` around an inline repair) are structure the
        # reader doesn't care about; a trace that IS one meaningful stage (foresight_rank, lessons)
        # naturally yields a single band with that label.
        by_sid = {s.get("span_id"): s for s in ss_sorted}
        root_sid = root["span_id"] if root else None

        def _stage_of(s):
            # Band identity, best evidence first:
            #   1. `phase_span` (tracing stamp): the innermost open operation's SPAN ID — exact
            #      sub-loop identity, live and post-hoc, two same-phase retries stay separate.
            #   2. an eval pipeline stage op (train/score — carries `stage`): its own band.
            #   3. pre-phase_span traces: nearest ancestor op matching the `phase` name (post-hoc),
            #      else a band synthesized from the bare phase name (live, op not flushed yet).
            #   4. no phase at all (old traces): the top-level sub-op under the root, else the root.
            a = s.get("attributes") or {}
            ph, ph_sid = a.get("phase"), a.get("phase_span")
            if ph_sid:
                op = by_sid.get(ph_sid)
                return op if op is not None else {"span_id": ph_sid, "name": ph,
                                                  "start": s.get("start", 0.0)}
            if s.get("kind") == "operation" and a.get("stage"):
                return s
            top_op, ph_op = None, None
            for cur in _iter_parent_spans(s, by_sid, stop_id=root_sid):
                if cur.get("kind") == "operation":
                    top_op = cur
                    if ph_op is None and ph and cur.get("name") == ph:
                        ph_op = cur       # nearest ancestor op matching the stamp: the real sub-loop
            if ph_op is not None:
                return ph_op
            if ph:
                return {"span_id": f"phase:{ph}", "name": ph, "start": s.get("start", 0.0)}
            return top_op or root or {"span_id": f"trace:{tid}",
                                      "name": (first or {}).get("name"),
                                      "start": s.get("start", 0.0)}

        groups: dict = {}
        for s in ss_sorted:
            if root_sid is not None and s.get("span_id") == root_sid:
                continue
            stg = _stage_of(s)
            groups.setdefault(stg.get("span_id"), {"span": stg, "spans": []})["spans"].append(s)
        # Order + timestamp bands by their first CONTENT span's start, not the op span's: the
        # Developer's stages/plan phases run INSIDE the orchestrator's `implement` span, so implement
        # OPENS first even though its own turns come last — sorting by op-span start would show the
        # implement band before the stages band whose turns actually happened first. (A NESTED op
        # span rides in its parent's group and would likewise drag the parent band's start back.)
        def _first_turn_start(g):
            return min((s.get("start", 0.0) for s in g["spans"] if s.get("kind") != "operation"),
                       default=g["span"].get("start", 0.0))
        for g in sorted(groups.values(), key=_first_turn_start):
            grp = sorted(g["spans"], key=lambda x: x.get("start", 0.0))
            turns = _thread_turns(grp, by_id)
            # Keep a stage band that is still RUNNING even though it has no turns yet: a live training
            # subprocess emits only the `stage_started` anchor (its own turn is suppressed as noise) and
            # its stage op flushes on close — so without this the Train/Evaluate band would be dropped as
            # "empty" for the whole run and only appear once the stage finished. The UI renders the live
            # stage log inside the (turnless) band.
            running_stage = any(s.get("name") == "stage_started" for s in grp)
            if turns or running_stage:
                stages.append({"trace_id": tid, "label": g["span"].get("name"),
                               "start": _first_turn_start(g),
                               "rollup": _rollup(grp), "turns": turns})
    stages.sort(key=lambda x: x.get("start", 0.0))
    total_stages = len(stages)
    total_turns = sum(len(stage.get("turns") or []) for stage in stages)
    # CODEX AGENT: Bound the rendered thread globally, not merely each text field.  A crafted trace
    # with thousands of tiny stages/turns otherwise remains a multi-megabyte response and DOM tree.
    visible: list[dict] = []
    remaining = _CONVERSATION_TURN_CAP
    for stage in reversed(stages[-_CONVERSATION_STAGE_CAP:]):
        turns = stage.get("turns") or []
        keep = turns[-remaining:] if remaining else []
        omitted_here = max(0, len(turns) - len(keep))
        if keep or not turns:
            visible.append({**stage, "turns": keep,
                            "projection": {"truncated": omitted_here > 0,
                                           "omitted_turns": omitted_here}})
        remaining = max(0, remaining - len(keep))
    stages = list(reversed(visible))
    visible_turns = sum(len(stage.get("turns") or []) for stage in stages)
    # `_observed_total` is the exact number of observations in traces attributed to this node for both
    # the whole-run fallback and the node-scoped index path; it is measured before the response cap.
    reported_total = max(matching_span_count, _observed_total, _projection_counter(total_spans))
    projection = _response_projection(
        total_spans=reported_total, visible_spans=len(matching_spans),
        truncated_spans=sum(1 for span in matching_spans
                            if (span.get("_projection") or {}).get("truncated")),
        total_stages=total_stages, visible_stages=len(stages),
        omitted_stages=max(0, total_stages - len(stages)),
        total_turns=total_turns, visible_turns=visible_turns,
        omitted_turns=max(0, total_turns - visible_turns))
    return {"schema": TRACE_PROJECTION_SCHEMA, "run_id": state.run_id, "task_id": state.task_id,
            "node_id": str(node_id), "stages": stages, "projection": projection}


def build_trace_view(state: RunState, spans: list[dict], *, light: bool = False,
                     total_spans=None, span_cap: int = TRACE_VIEW_SPAN_CAP) -> dict:
    """Group spans into per-node trees + a run summary, correlated by node_id (carried on each
    trace's root span). Spans with no node_id land under `unscoped` (e.g. onboarding). Each span
    carries `kind` (operation/generation/tool) so the UI renders the Langfuse-style observation
    tree; `rollups` gives per-node token/cost/observation totals aggregated from generations.
    Heavy generation I/O is truncated (see `_cap_span_io`) so the payload stays browser-safe; with
    `light=True` it's dropped entirely (run-level timeline doesn't need prompts/outputs)."""
    selected, observed_total = _bounded_tail(spans, span_cap)
    spans = [(_strip_span_io if light else _cap_span_io)(s) for s in _normalize_spans(selected)]
    reported_total = max(observed_total, _projection_counter(total_spans), len(spans))
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)

    # Resolve each span's EFFECTIVE node: its own stamped node_id, else the node_id on its trace's ROOT.
    # node_id is now stamped PER-SPAN (tracing._node_ctx), so a single long-lived Developer tool-loop
    # trace — which serves several nodes in sequence — splits correctly across them by each span's own
    # id. The root fallback (NOT a full ancestor walk, which would bleed one node's id onto the whole
    # of a shared trace) keeps OLD root-only logs working: a create_node trace whose children carry no
    # id attributes to its root's node. Spans with neither → `unscoped`.
    root_nid: dict[str, Optional[int]] = {}
    for tid, sps in by_trace.items():
        f = _tree(sps, _normalized=True)
        root_nid[tid] = _node_id_of(f[0]) if f else None

    node_spans: dict[str, list[dict]] = defaultdict(list)
    unscoped_spans: list[dict] = []
    for s in spans:
        nid = _node_id_of(s)
        if nid is None:
            nid = root_nid.get(s.get("trace_id"))
        (node_spans[str(nid)] if nid is not None else unscoped_spans).append(s)
    nodes: dict[str, list[dict]] = {
        nid: _tree(sps, _normalized=True) for nid, sps in node_spans.items()
    }
    unscoped = _tree(unscoped_spans, _normalized=True)

    errors = [s for s in spans if s.get("status") == "ERROR"]
    run_roll = _rollup(spans)
    truncated_spans = sum(
        1 for span in spans if (span.get("_projection") or {}).get("truncated") is True)
    projection = _response_projection(
        total_spans=reported_total, visible_spans=len(spans), light=light,
        truncated_spans=truncated_spans)
    return {
        "schema": TRACE_PROJECTION_SCHEMA,
        "run_id": state.run_id,
        "task_id": state.task_id,
        "nodes": {k: v for k, v in nodes.items()},
        "rollups": {k: _rollup(v) for k, v in node_spans.items()},
        "unscoped": unscoped,
        "projection": projection,
        "summary": {
            "spans": reported_total,
            "visible_spans": len(spans),
            "omitted_spans": projection["omitted_spans"],
            "rollup_partial": projection["omitted_spans"] > 0,
            "errors": len(errors),
            "generations": run_roll["generations"],
            "tools": run_roll["tools"],
            "tokens": run_roll["tokens"],
            "cost": run_roll["cost"],
            "total_eval_seconds": round(state.total_eval_seconds, 3),
        },
    }
