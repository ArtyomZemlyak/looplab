"""UI projection of the trace (ADR-17): join the research tree (from `events.jsonl` → RunState)
to its execution detail (from `spans.jsonl`). Pure reader of files-as-truth — never a source of
truth. Produces a per-node span forest the HTML view and the future React UI both consume.

Spans nest by (trace_id, span_id, parent_id); each top-level operation is its own trace tagged
with node_id (see tracing.py), so we group traces by node_id and build a child tree per trace.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections import defaultdict, deque
from itertools import islice
from typing import Optional

from looplab.core.models import RunState
from looplab.core.redact import is_secret_key_name, redact_persisted_text
from looplab.core.trace_files import (
    TRACE_JSONL_ROW_MAX_BYTES,
    iter_bounded_trace_jsonl_lines as _iter_bounded_trace_jsonl_lines,
    open_private_trace_file, trace_file_change_token)


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
# The ONE ceiling for the UI's "load more" control on a single node — BOTH per-node surfaces (the span
# tree and the linear conversation) page against this number. The default caps stay 512 (fast expand),
# but a user can page a heavily-repaired node up to this bound on demand. Still O(node) — a bigger cap
# only surfaces more of THAT node's already-scoped spans (see appstate.node_trace_view). A second
# ceiling beside this one is how the two surfaces silently stop agreeing on what "everything" means.
TRACE_NODE_SPAN_CAP_MAX = 4096
TRACE_DETAIL_SPAN_CAP = 256
# The card-trace projection's two section ceilings. Every sibling projection in this file bounds
# what it returns; these loops did not, and the serve caller hands them the WHOLE run's light span
# list — so a crafted `spans.jsonl` with thousands of root `propose` spans stamped with one card_id
# (`card_id` survives normalization) produced an unbounded HTTP payload. Generous relative to any
# real card (a card's research rows are its proposal attempts, its node rows its attempts) so the
# cap is invisible in practice and the receipt discloses it when it is not.
TRACE_CARD_RESEARCH_CAP = 256
TRACE_CARD_NODE_CAP = 256
TRACE_CONVERSATION_SPAN_CAP = 512
# Trace projections are already bounded by the span and per-field caps above.  This final aggregate
# ceiling keeps both HTTP responses and the archived trace.json finite without imposing a topology
# depth limit (a 4,096-deep valid tree is still part of the public projection contract).
TRACE_PROJECTION_JSON_MAX_BYTES = 64 * 1024 * 1024


def trace_projection_json_bytes(
        value, *, max_bytes: int = TRACE_PROJECTION_JSON_MAX_BYTES) -> bytes:
    """Encode one normalized trace projection without using the Python call stack.

    FastAPI's generic encoder and common JSON encoders recurse through nested containers, so a valid
    deep span chain can fail after the iterative tree builder has successfully preserved it.  Trace
    projections contain only JSON-native containers/scalars; an explicit stack keeps their nested
    wire shape byte-for-byte conventional while the aggregate byte ceiling bounds the result.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    output = bytearray()
    active: set[int] = set()

    def append(chunk: bytes) -> None:
        if len(output) + len(chunk) > max_bytes:
            raise ValueError("trace projection exceeds its JSON byte limit")
        output.extend(chunk)

    # Frames are (kind, value, ...). Iterator frames are resumed after each child, so memory grows
    # with topology depth rather than with the total number of siblings.
    stack: list[tuple] = [("value", value)]
    while stack:
        frame = stack.pop()
        kind = frame[0]
        if kind == "value":
            current = frame[1]
            current_type = type(current)
            if current_type is dict:
                identity = id(current)
                if identity in active:
                    raise ValueError("circular trace projection")
                active.add(identity)
                append(b"{")
                stack.append(("dict", iter(current.items()), True, identity))
            elif current_type in (list, tuple):
                identity = id(current)
                if identity in active:
                    raise ValueError("circular trace projection")
                active.add(identity)
                append(b"[")
                stack.append(("list", iter(current), True, identity))
            elif current is None or current_type in (bool, int, float, str):
                append(json.dumps(
                    current, ensure_ascii=False, allow_nan=False,
                    separators=(",", ":")).encode("utf-8"))
            else:
                raise TypeError(
                    f"unsupported trace projection value: {current_type.__name__}")
        elif kind == "dict":
            iterator, first, identity = frame[1:]
            try:
                key, child = next(iterator)
            except StopIteration:
                append(b"}")
                active.remove(identity)
                continue
            if type(key) is not str:
                raise TypeError("trace projection object keys must be strings")
            if not first:
                append(b",")
            append(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            append(b":")
            stack.append(("dict", iterator, False, identity))
            stack.append(("value", child))
        else:
            iterator, first, identity = frame[1:]
            try:
                child = next(iterator)
            except StopIteration:
                append(b"]")
                active.remove(identity)
                continue
            if not first:
                append(b",")
            stack.append(("list", iterator, False, identity))
            stack.append(("value", child))
    return bytes(output)


def settle_node_span_cap(limit, *, default: int) -> int:
    """The ONE settle rule for a client-supplied per-node span window (`?limit=`).

    `limit` arrives from the wire, so it is settled rather than trusted. Absent/`0` and any
    non-integral or negative value mean "no explicit request" and keep `default` — a malformed limit
    must never widen a window by accident. A real request can only ever RAISE the window (never shrink
    it below the default a client did not ask about), and every request is clamped to
    ``TRACE_NODE_SPAN_CAP_MAX`` so one pathological node can never materialize an unbounded tree.

    Both per-node readers route through this (`appstate.node_trace_view` and the conversation route):
    a second copy of "default 512, floor 512, ceiling 4096" is exactly how the span tree and the
    conversation would end up disagreeing about how far a single "load more" click reaches.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return default
    return max(default, min(limit, TRACE_NODE_SPAN_CAP_MAX))


def trace_file_revision(path: str | os.PathLike) -> Optional[str]:
    """Cheap CAS token for the exact ``spans.jsonl`` file snapshot.

    Node-detail reads compare this token before and after projection; destructive trace operations
    compare it again while owning ``engine.lock``. File identity plus content metadata catches both
    append-in-place writers and atomic replacements without hashing a diagnostics file that may be
    gigabytes large.
    """
    try:
        # The revision is part of the destructive trace-clear confirmation.  It must describe the
        # run's own regular sidecar, not the target of a symlink/hardlink (and opening a FIFO without
        # O_NONBLOCK can strand a server worker before clear even begins).
        with open_private_trace_file(path, open_file=open) as stream:
            source_stat = os.fstat(stream.fileno())
            change_token = trace_file_change_token(stream.fileno(), source_stat)
    except FileNotFoundError:
        return hashlib.sha256(b"looplab:spans:missing:v1").hexdigest()
    except OSError:
        return None
    if change_token is None:
        # An opaque validator that cannot distinguish same-file rewrites is unsafe authority for
        # destructive clear approval.  The caller must reject the operation and ask for a new,
        # provable snapshot rather than accept Windows creation time as a mutation fence.
        return None
    identity = (
        int(source_stat.st_dev), int(source_stat.st_ino), change_token,
        int(source_stat.st_size), int(source_stat.st_mtime_ns),
    )
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode("ascii")).hexdigest()

_SPAN_TEXT_BUDGET = 8192
_META_TEXT_CAP = 256
_EVENTS_CAP = 16
_EVENT_FIELDS_CAP = 8
_STRUCT_ITEMS_CAP = 32
_STRUCT_DEPTH_CAP = 3
_TOOL_CALLS_CAP = 16
_CONVERSATION_STAGE_CAP = 64
_CONVERSATION_TURN_CAP = 256


def conversation_render_caps(span_cap: int) -> tuple[int, int]:
    """Stages/turns to RENDER for a given conversation span window — they scale WITH the window.

    Measured on `runs/rubert-dr-0804` node 1 (14,507 spans) before this existed: at the default
    window the response carried 512 spans, and from those 512 spans the projection derived 256 stages
    and 425 turns — of which it rendered 64 and 105. Every one of the missing 192 stages was already
    IN HAND; only these two caps hid them. So raising the span window alone was a placebo: `?limit=`
    at 1024 and at 4096 both returned the byte-identical 64-stage response, because the caps below
    re-truncated the wider read back to the same 64.

    That is why the conversation's "load more" moves all three numbers together instead of the span
    window alone. The window stays the single knob (and TRACE_NODE_SPAN_CAP_MAX stays the single
    ceiling); these caps are DERIVED from it, so the response size stays proportional to what the
    operator explicitly asked for — measured 197 KB at x1 up to 1.6 MB at the x8 ceiling.
    """
    factor = max(1, int(span_cap) // TRACE_CONVERSATION_SPAN_CAP)
    return _CONVERSATION_STAGE_CAP * factor, _CONVERSATION_TURN_CAP * factor


def unavailable_projection(*, light: bool | None = None) -> dict:
    """Projection receipt for a source that could not be read at all."""
    # unavailable cardinality is unknown. Never turn an I/O failure into plausible zero
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
    "node_id", "generation", "phase", "phase_span", "input_from", "input_carry", "input_partial",
    # The Researcher's CARD binding (orchestrator.stamp_proposal_span). `card_id` is the join that
    # makes a card's own research reachable; `proposed_for_node` is the node the proposal was
    # prepared for and is deliberately NOT `node_id`, which would re-attribute the whole trace to
    # one node. Both must be on this allowlist or the projection silently drops them — which is
    # exactly what happened the first time, and made the stamp look like it had never run.
    "card_id", "proposed_for_node",
    # generation / tool observation
    "model", "op", "model_parameters", "tool", "tool_calls", "input", "output",
    "thinking", "usage", "cost", "level",
    # engine/evaluation operation diagnostics used by the Inspector
    "stage", "exit_code", "timed_out", "reused", "sandboxed", "seed", "blocks",
    "attempt", "reason", "package", "trigger", "operator", "parent_id", "proxy_score",
    "proxy_skipped", "eval_seconds", "metric", "ok", "repair_attempts", "violations",
    "drift", "error_reason", "feasible", "robust_metric", "materialized",
    "handoff_from", "handoff_to",
    # Bounded internal exporter-health receipts.  These are diagnostic metrics, not domain events;
    # retaining only their fixed schema lets the run summary expose loss without trusting arbitrary
    # custom-exporter attributes or requiring clients to parse internal spans.
    "looplab.exporter.metric", "looplab.exporter.dropped_spans",
    "looplab.exporter.export_failures", "looplab.exporter.queue_capacity_spans",
    "looplab.exporter.queue_capacity_bytes", "looplab.exporter.instance_id",
    "looplab.exporter.pid", "looplab.exporter.dropped.queue_full",
    "looplab.exporter.dropped.queue_bytes", "looplab.exporter.dropped.serialization_error",
    "looplab.exporter.dropped.worker_start",
    "looplab.exporter.dropped.shutdown", "looplab.exporter.dropped.shutdown_timeout",
}
_ATTR_TEXT_FIELDS = {
    "phase", "model", "op", "tool", "level", "stage", "reason", "package", "trigger",
    "operator", "error_reason", "materialized", "handoff_from", "handoff_to", "card_id",
    "looplab.exporter.metric", "looplab.exporter.instance_id",
}
_ATTR_BOOL_FIELDS = {
    "input_partial", "timed_out", "reused", "sandboxed", "proxy_skipped", "ok", "drift",
    "feasible",
}
_ATTR_INT_FIELDS = {
    "generation", "input_carry", "exit_code", "seed", "blocks", "attempt",
    "repair_attempts", "violations", "proposed_for_node",
    "looplab.exporter.dropped_spans", "looplab.exporter.export_failures",
    "looplab.exporter.queue_capacity_spans", "looplab.exporter.queue_capacity_bytes",
    "looplab.exporter.pid", "looplab.exporter.dropped.queue_full",
    "looplab.exporter.dropped.queue_bytes", "looplab.exporter.dropped.serialization_error",
    "looplab.exporter.dropped.worker_start",
    "looplab.exporter.dropped.shutdown", "looplab.exporter.dropped.shutdown_timeout",
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
    # WHY the four loops below iterate `sorted(...)`: _ATTR_TEXT_FIELDS / _ATTR_BOOL_FIELDS /
    # _ATTR_INT_FIELDS / _ATTR_FLOAT_FIELDS are SETS, so inserting into `attributes` in raw set order
    # is string-hash order — randomized per process via PYTHONHASHSEED. That made the serialized
    # projection (and any persisted index record built from it) not byte-stable across two server
    # runs, the same raw-set-iteration defect this change series fixed in
    # project_hierarchy/project_lens (now search/concept_lens.py). Keep the sort (or make the groups tuples).
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


def _bounded_node_trace_tail(values, node_id, cap: int, *,
                             generation: Optional[int] = None,
                             _normalized: bool = False) -> tuple[list, int]:
    """Cap a node conversation only after selecting the traces attributed to that node.

    The no-index path receives the entire run, whereas the indexed path already receives only spans
    effectively attributed to the target node. Taking the whole-run or candidate-trace tail first
    lets sufficiently busy unrelated/shared-trace rows evict the target and falsifies its total.
    ``build_conversation`` is public but its normal inputs are concrete snapshots (``load_spans`` or
    ``SpanIndex.full_spans_for_node``); retain a bounded one-pass degradation for exotic iterables.
    """
    if not isinstance(values, (list, tuple)):
        return _bounded_tail(values, cap)

    target = str(node_id)
    records: list[tuple[dict, dict]] = []
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for raw in values:
        if not isinstance(raw, dict):
            continue
        normalized = raw if _normalized else _normalize_span(raw)
        if normalized is None:
            continue
        trace_id = normalized.get("trace_id")
        records.append((raw, normalized))
        by_trace[trace_id].append(normalized)

    # ONE root resolution per trace, read twice — the same rule as `span_index._rows_for_node`,
    # which this function is the no-index half of. Calling both `trace_root_*` helpers rebuilt the
    # whole `by_id`/roots/min derivation twice per trace, and here that is over the WHOLE run's
    # spans rather than one node's.
    trace_meta = {
        trace_id: (root_span_node_id(root), root_span_generation(root))
        for trace_id, root in (
            (tid, trace_root_span(spans, _normalized=True))
            for tid, spans in by_trace.items())
    }
    # Filtering precedes the global cap and exact total. Candidate-by-any-stamped-row is not enough:
    # one long-lived trace can carry spans for several nodes, and its newest foreign row must not
    # consume the target node's window. Keep this identical to SpanIndex._rows_for_node.
    matching = (
        raw for raw, normalized in records
        if (meta := trace_meta.get(normalized.get("trace_id"))) is not None
        and (generation is None or meta[1] == generation)
        and (effective := effective_node_id(normalized, meta[0])) is not None
        and str(effective) == target
    )
    return _bounded_tail(matching, cap)


def _response_projection(*, total_spans: int, visible_spans: int, light: bool = False,
                         truncated_spans: int = 0, **extra) -> dict:
    total = max(visible_spans, _projection_counter(total_spans))
    omitted = max(0, total - visible_spans)
    clean_extra = {key: _projection_counter(value) for key, value in extra.items()}
    # `truncated` is the flag a reader checks BEFORE trusting a section, so it has to be true when
    # ANY axis is short — not only the span axis and the explicit `omitted_*` counters. A caller
    # that passes `total_x`/`visible_x` for its own axis (the card projection's research and node
    # sections) was reporting `truncated: False` over a capped list, which is exactly the
    # "a truncated projection indistinguishable from a complete one" this receipt exists to prevent.
    short_axis = any(
        clean_extra.get(f"visible_{axis}", 0) < value
        for key, value in clean_extra.items()
        if key.startswith("total_") and (axis := key[len("total_"):]))
    truncated = omitted > 0 or truncated_spans > 0 or short_axis or any(
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
    """Read the bounded JSONL prefix, quarantining valid objects with an invalid span shape.

    A physical row larger than :data:`TRACE_JSONL_ROW_MAX_BYTES` ends the readable prefix; it is
    neither materialized nor silently skipped to expose later rows behind an untrusted envelope.
    """
    from looplab.events.eventstore import JsonlRecordInvalid, decode_jsonl_line

    def records(stream):
        for raw in _iter_bounded_trace_jsonl_lines(
                stream, max_line_bytes=TRACE_JSONL_ROW_MAX_BYTES):
            try:
                value = decode_jsonl_line(raw)
            except JsonlRecordInvalid:
                break
            if value is not None:
                yield value

    try:
        # One hardened descriptor is both the readability proof and the bytes.  The former preflight
        # plus `iter_jsonl(path)` resolved the pathname twice and followed links on both resolutions.
        with open_private_trace_file(path, open_file=open) as stream:
            return _normalize_spans(records(stream))
    except FileNotFoundError:
        return []


def load_span_tail(
        path: str | os.PathLike,
        cap: int = TRACE_VIEW_SPAN_CAP,
) -> tuple[list[dict], int]:
    """Stream a normalized span-log prefix, retaining only its newest ``cap`` admitted rows.

    This is the bounded reader for static/final projections.  It deliberately shares ``iter_jsonl``'s
    append-log rule: a blank line is consumed, a complete JSON-object row with an invalid span shape is
    quarantined individually, and a torn, invalid-JSON, or non-object row ends the readable prefix.  The
    returned count is the exact number of normalized spans admitted in that prefix, including rows older
    than the retained tail.  A missing sidecar is known-empty; every other open/read error propagates so
    callers can distinguish unavailable telemetry from an exact zero.

    Peak retained memory is the normalized tail plus one physical JSONL row bounded by
    :data:`TRACE_JSONL_ROW_MAX_BYTES`. An oversized row ends this readable prefix (and the returned
    count describes exactly the admitted prefix); it is never skipped to make unproven later bytes
    appear valid. In particular, finalization must not materialize and hydrate a multi-gigabyte
    ``spans.jsonl`` — or one multi-gigabyte legacy/corrupt row — merely to have
    :func:`build_trace_view` discard everything before its bounded response window.
    """
    from looplab.events.eventstore import JsonlRecordInvalid, decode_jsonl_line

    settled_cap = max(0, int(cap))
    tail: deque[dict] = deque(maxlen=settled_cap)
    total = 0
    try:
        with open_private_trace_file(path, open_file=open) as stream:
            for raw in _iter_bounded_trace_jsonl_lines(
                    stream, max_line_bytes=TRACE_JSONL_ROW_MAX_BYTES):
                try:
                    value = decode_jsonl_line(raw)
                except JsonlRecordInvalid:
                    break
                if value is None:
                    continue
                normalized = _normalize_span(value)
                if normalized is None:
                    continue
                total += 1
                tail.append(normalized)
    except FileNotFoundError:
        return [], 0
    return list(tail), total


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


def trace_root_span(spans: list[dict], *, _normalized: bool = False) -> Optional[dict]:
    """The ROOT span of one trace — the ONE root-resolution rule (doc 25 EV-10). None if rootless.

    ROOT means "parent not present in this trace" — a true `parent_id is None` span OR an ORPHAN
    whose parent is missing. The orphan case is not exotic, it is the normal LIVE shape: an operation
    span is written only on CLOSE and `create_node` closes at node END, so for the whole life of a
    node its trace has no root on disk and every span in it is an orphan. A span QUARANTINED by the
    index (`span_index._scan_light` drops a record that fails `_normalize_span` but still consumes
    it) orphans its children the same way, and that orphan outlives its parent's close.

    A trace routinely holds SEVERAL roots — one trace_id spans a whole sequence of operations, and
    every span under a still-open parent is an orphan until that parent closes — so "the root" is a
    CHOICE among them. The choice is the earliest `start`, NEVER file order: spans.jsonl is written
    in CLOSE order, so the span that OPENED a trace is usually written LAST, and concurrent spans
    finish out of the order they began. Measured on a real log: in `runs/live-deps4-0804`, trace
    `c49e58adeb726df798e4d6182855ab7d`, a concurrent LLM fan-out under one open operation span made
    the two orders pick DIFFERENT roots at four consecutive index states.

    Equivalent to `_tree(spans, ...)[0]` by construction — `_tree` collects exactly this root set and
    sorts each level by `start`, and both `min` and that stable sort keep the FIRST minimum in
    `by_id` order — but derived WITHOUT building the forest, because the forest copies every span
    dict (`{**s, "children": []}`) and this runs on `span_index`'s per-node read path over a file
    that is routinely hundreds of MB. `tests/test_span_index.py` pins the two against each other.

    Read-only: this returns a span from `spans`, not a `_tree` node, so it carries no `children`.
    """
    if not _normalized:
        spans = _normalize_spans(spans)
    # Keyed by span_id exactly as `_tree` does, so a duplicated span_id collapses to its LAST
    # occurrence in both — the root set must not depend on which of the two derivations you asked.
    by_id = {s["span_id"]: s for s in spans}
    roots = [s for s in by_id.values() if s.get("parent_id") not in by_id]
    # Rootless means every span's parent is present, which in a finite set means a CYCLE (only
    # reachable from a corrupt/crafted spans.jsonl). `_tree` returns no roots there rather than
    # nominating an arbitrary span, and so does this: a caller that cannot name a root must fall
    # back explicitly, not silently read one span's attributes as if they were the trace's.
    return min(roots, key=lambda s: s.get("start", 0.0)) if roots else None


def trace_root_generation(spans: list[dict], *, _normalized: bool = False) -> int:
    """Lifecycle attempt stamped on a trace root; legacy/rootless traces belong to attempt zero.

    Generation is a trace property, not a descendant's incidental retry ``attempt``. Keeping this
    beside ``trace_root_span`` makes the indexed and fallback conversation readers choose the same
    root and therefore the same lifecycle even though spans.jsonl is written in close order.
    """
    return root_span_generation(trace_root_span(spans, _normalized=_normalized))


def root_span_generation(root: Optional[dict]) -> int:
    """The generation OF an already-resolved root span — the field read, split from the resolution.

    A caller that needs both a trace's generation and its node id (`span_index._rows_for_node`,
    `traceview._bounded_node_trace_tail`) otherwise calls `trace_root_span` TWICE over the same list,
    and that helper rebuilds a `by_id` dict plus a roots comprehension plus a `min` every time. On a
    14,507-span trace that is two full dict builds per candidate trace, per request. Same rule, one
    resolution: this is the accessor half, `trace_root_span` is the choice half.
    """
    attributes = root.get("attributes") if root is not None else None
    value = attributes.get("generation") if isinstance(attributes, dict) else None
    return value if type(value) is int and value >= 0 else 0


def trace_root_node_id(spans: list[dict], *, _normalized: bool = False) -> Optional[int | str]:
    """The node a trace as a whole belongs to: the `node_id` of its ROOT span, or None.

    The ONE definition, because two of them disagreed (doc 25 EV-10). `build_conversation` derived
    attribution from its structural `root` (strictly `parent_id is None`) while `build_trace_view`
    used `_tree`'s first root, and a comment asserted the two were "exactly" the same. On a trace
    holding both an orphan and a later true root they pick DIFFERENT spans, so a span carrying no
    node_id of its own was attributed to one node in the trace view and a different node in the
    conversation — the same defect class the conversation comment already records once.

    Never a full ancestor walk: that would bleed one node's id across a shared trace, which is the
    thing per-span stamping exists to prevent.
    """
    return root_span_node_id(trace_root_span(spans, _normalized=_normalized))


def root_span_node_id(root: Optional[dict]) -> Optional[int | str]:
    """The node id OF an already-resolved root span — see `root_span_generation` for why this half
    is separable: a caller needing both facts resolves the root ONCE and reads it twice."""
    return _node_id_of(root) if root is not None else None


def effective_node_id(span: dict, trace_root_nid: Optional[int | str]) -> Optional[int | str]:
    """A span's own stamped node, else the node of its trace root. The attribution rule itself.

    Per-span first, because `node_id` is stamped per span (`core/tracing._node_ctx`): one long-lived
    Developer tool-loop trace serves several nodes in sequence, so keying the whole trace off its
    root drops a node's turns from its own view and hands them to the root's node. The root fallback
    is what keeps OLD root-only logs working, where children carry no id at all.
    """
    own = _node_id_of(span)
    return own if own is not None else trace_root_nid


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


def _exporter_loss_rollup(spans: list[dict]) -> dict[str, int]:
    """Sum the coalesced exporter-loss deltas retained in this projection window.

    A receipt is deliberately an internal diagnostic span rather than a replay/domain event.  Each
    receipt reports only the delta consumed by that one durable attempt, so the visible sum is exact
    for the receipts actually present. It may still be a lower bound on process-local loss when a
    receipt attempt failed ambiguously; retrying that delta would risk double-counting a committed
    append. ``build_trace_view`` separately marks the result partial whenever older receipts may have
    been omitted by its bounded tail.
    """
    dropped = failures = receipts = 0
    for span in spans:
        if span.get("name") != "looplab.exporter.loss":
            continue
        raw_attributes = span.get("attributes")
        attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
        if attributes.get("looplab.exporter.metric") != "loss":
            continue
        dropped = min(
            _MAX_TRACE_TOKENS,
            dropped + _safe_token_count(attributes.get("looplab.exporter.dropped_spans")),
        )
        failures = min(
            _MAX_TRACE_TOKENS,
            failures + _safe_token_count(attributes.get("looplab.exporter.export_failures")),
        )
        receipts = min(_MAX_TRACE_TOKENS, receipts + 1)
    return {"dropped_spans": dropped, "export_failures": failures,
            "loss_receipts": receipts}


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


def strip_span_io(s: dict) -> dict:
    """Drop the heavy I/O entirely — the run-level trace (Dock timeline) needs only structure, timing,
    model + token usage, not the prompts/outputs. Keeps the whole-run payload tiny; detail endpoints
    serve a bounded/redacted diagnostic projection for the Inspector. Also drops the delta bookkeeping
    (`input_carry`/`input_from`): with no `input` they carry no meaning, and leaving them would let a
    stray `hydrate_inputs` on a light span reconstruct to `[]` (its `input` is gone)."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in _STRIP_IO_KEYS):
        return s
    return {**s, "attributes": {k: v for k, v in a.items() if k not in _STRIP_IO_KEYS}}


# Back-compatible internal seam: the span index and existing tests live in the same events package,
# while cross-package callers use the public spelling above instead of growing private-import debt.
_strip_span_io = strip_span_io


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
            # A DROP is always a reset. EQUALITY is only evidence when this generation's count is the
            # TRUE one: `_project_messages` caps `input` at _MSGS_CAP and marks the capped span
            # `input_partial`, so on a long old log every generation past the cap plateaus at
            # n == prev_in == _MSGS_CAP — reading that as a boundary re-emitted the "request" turn on
            # EVERY turn, exactly the re-duplication this projection exists to remove. (A capped
            # PREVIOUS against an uncapped equal current is a real drop — true prev > cap == n — so
            # only the current span's capping is disqualifying.)
            is_base = (a.get("input_carry") == 0) if ("input_carry" in a) \
                else (prev_in is None or n < prev_in
                      or (n == prev_in and a.get("input_partial") is not True))
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


def hydrate_inputs(spans: list[dict], *, _normalized: bool = False) -> list[dict]:
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
    prompt projection.

    `_normalized` is the same internal contract as `_tree`'s: pass it ONLY when every span provably
    came straight from `load_spans` or `SpanIndex._read_full`, both of which already ran
    `_normalize_span`. Normalization runs redaction/entropy analysis over every text field, and the
    finalize path used to pay that pass three times over (load_spans, here, then build_trace_view).
    The pass AFTER hydration is not redundant and is never skipped: a reconstructed `input` is new
    content that has not been through the projection budget."""
    if not _normalized:
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
        # (span_id, carry, delta_input, own_partial), from `sid` upward. `own_partial` is per-LEVEL
        # rather than one flag for the whole walk, because partialness travels in exactly one
        # direction: a span whose input was dropped makes every span chained ONTO it truncated, and
        # says nothing about the ancestors above it, whose reconstructions are still exact. A single
        # accumulator applied uniformly over `reversed(chain)` marked the whole chain — so walking
        # the same three spans leaf-first flagged the complete BASE as partial, i.e. the answer
        # depended on file order. Both directions were wrong, in opposite ways.
        chain: list[tuple] = []
        seen: set = set()
        cur_sid = sid
        base: list = []
        broke = False                     # partialness of the BASE the walk bottoms out at
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
            # A durable exporter fallback can retain this span's delta identity while omitting its
            # over-limit input. Once one ancestor declares that loss, every descendant is partial
            # even though the ancestor row and back-reference are both present and well-formed.
            own_partial = a.get("input_partial") is True
            cur = a.get("input")
            if "input_carry" not in a or not isinstance(cur, list):
                base = cur if isinstance(cur, list) else []
                break   # old log / non-list → input IS full
            frm = a.get("input_from")
            if frm is None:                            # self-contained base: its `input` is the full ctx
                memo[cur_sid] = list(cur)
                partial[cur_sid] = own_partial         # an explicit exporter/projection loss survives
                base = memo[cur_sid]
                broke = own_partial
                break
            # Coerce carry to a NON-NEGATIVE int: a malformed span (bit-rot on a network mount, or a
            # hand-edited log) whose input_carry is a string/float would make `full[:carry]` raise
            # TypeError and abort the WHOLE trace, and a negative carry would silently truncate the
            # prefix. Fall back to 0 (the delta stands as the full input) — the safe degradation the
            # non-list/absent-carry branch above already uses — instead of crashing the projection.
            raw_carry = a.get("input_carry")
            carry = raw_carry if (isinstance(raw_carry, int) and not isinstance(raw_carry, bool)
                                  and raw_carry >= 0) else 0
            chain.append((cur_sid, carry, cur, own_partial))
            cur_sid = frm
        full = base
        running = broke
        for csid, carry, delta, own_partial in reversed(chain):
            # base→leaf, memoizing every level. `running` accumulates DOWNWARD only: once a level
            # declares its own loss, it and everything chained onto it are partial; levels already
            # passed stay exact.
            full = list(full[:carry]) + list(delta)
            running = running or own_partial
            memo[csid] = full
            partial[csid] = running
        if sid not in memo:
            memo[sid] = full
            partial[sid] = running
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


def _conversation_bands(spans: list[dict], *, keep) -> tuple[list[dict], list[dict]]:
    """Bands + threaded turns over an ALREADY-SELECTED, already-normalized span set.

    `keep(span, trace_node_id) -> bool` is the only thing that differs between the two conversations
    this serves: the per-NODE one keeps the spans attributed to its node, the per-TRACE one keeps
    everything in the trace it was handed. Everything below — how a trace splits into sub-loop bands,
    how turns are threaded and de-duplicated, which band a live-but-unclosed operation belongs to —
    is the same reading and is deliberately written once. The card surface used to open a bare span
    tree instead precisely because this half was unreachable from anywhere but a node id.

    Returns `(stages, matching_spans)`; the caller owns the render caps and the projection receipt.
    """
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
        # `first` is this trace's STRUCTURAL representative — it names the stage below and stands in
        # for a root that has not closed yet. It is deliberately NOT the attribution root: see the
        # next comment.
        first = root or (ss_sorted[0] if ss_sorted else None)
        # ATTRIBUTION comes from the shared rule, not from `root`. `root` is the STRUCTURAL container
        # this function also uses below (`root_sid`, the band fallback), and it requires
        # `parent_id is None`; the attribution root additionally accepts an ORPHAN, which is the
        # normal live shape described above. Deriving attribution from `root` therefore selected a
        # different span than `build_trace_view` did on any trace holding both — so the same span was
        # attributed to two different nodes in the two views, under a comment asserting the two were
        # "exactly" the same (doc 25 EV-10).
        trace_nid = trace_root_node_id(ss_sorted, _normalized=True)
        # Attribute PER SPAN: a span's own stamped node_id, else its trace's root node. node_id is
        # stamped per-span, so one long-lived Developer tool-loop trace can serve several nodes in
        # sequence — keying the whole trace off its ROOT then dropped the target node's turns from
        # its own conversation while handing them to the root's node, and the selection predicate
        # (`_bounded_node_trace_tail` / `SpanIndex.node_tids`, both ANY-span) still counted them, so
        # the loss surfaced as generic truncation rather than as the missing attribution it was.
        # Root-only legacy logs are unaffected: their spans have no own id and all fall back to the
        # same trace node.
        mine = [s for s in ss_sorted if keep(s, trace_nid)]
        if not mine:
            continue
        matching_spans.extend(mine)
        # Split EVERY trace into its sub-loop bands (propose / stages / plan / implement / repair /
        # inline_repair / …) so the conversation reads as ordered role blocks. Wrapper roots
        # (`create_node` = "Author node", `seed_workspace` around an inline repair) are structure the
        # reader doesn't care about; a trace that IS one meaningful stage (foresight_rank, lessons)
        # naturally yields a single band with that label.
        # Band resolution walks the WHOLE trace (`by_sid`/`root_sid`), not just this node's spans: the
        # enclosing operation that names a band may itself belong to the node that opened the trace.
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
        for s in mine:
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
    return stages, matching_spans


def _conversation_payload(state: RunState, stages: list[dict], matching_spans: list[dict], *,
                          observed_total: int, total_spans, span_cap: int,
                          identity: dict) -> dict:
    """The render caps + the omission receipt, shared by both conversations.

    `identity` names the SUBJECT this reading is of (`node_id` / `trace_id`) and is echoed so the
    browser can fence a late in-flight response from the previous subject — the same fence the node
    surface has always applied, now stated once for both.
    """
    stage_cap, turn_cap = conversation_render_caps(span_cap)
    total_stages = len(stages)
    total_turns = sum(len(stage.get("turns") or []) for stage in stages)
    # Bound the rendered thread globally, not merely each text field.  A crafted trace
    # with thousands of tiny stages/turns otherwise remains a multi-megabyte response and DOM tree.
    visible: list[dict] = []
    remaining = turn_cap
    for stage in reversed(stages[-stage_cap:]):
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
    # `observed_total` is the exact number of observations in traces attributed to this subject for
    # both the whole-run fallback and the index path; it is measured before the response cap.
    reported_total = max(len(matching_spans), observed_total, _projection_counter(total_spans))
    projection = _response_projection(
        total_spans=reported_total, visible_spans=len(matching_spans),
        truncated_spans=sum(1 for span in matching_spans
                            if (span.get("_projection") or {}).get("truncated")),
        total_stages=total_stages, visible_stages=len(stages),
        omitted_stages=max(0, total_stages - len(stages)),
        total_turns=total_turns, visible_turns=visible_turns,
        omitted_turns=max(0, total_turns - visible_turns))
    return {"schema": TRACE_PROJECTION_SCHEMA, "run_id": state.run_id, "task_id": state.task_id,
            **identity, "stages": stages, "projection": projection}


def build_conversation(state: RunState, spans: list[dict], node_id, *, total_spans=None,
                       span_cap: int = TRACE_CONVERSATION_SPAN_CAP,
                       generation: Optional[int] = None,
                       _normalized: bool = False) -> dict:
    """Per-node linear conversation (companion to `build_trace_view`). One `stage` per trace tagged
    with this node (create_node / evaluate / …), each a de-duplicated thread of turns. Reader of
    files-as-truth; caps every string for the browser, but never re-sends the growing history.

    `span_cap` is the UI's "load more" window (settled by `settle_node_span_cap` at the route). It
    widens the read AND, through `conversation_render_caps`, the stage/turn caps below in step — see
    that function for why moving only one of the three surfaces nothing."""
    selected, observed_total = _bounded_node_trace_tail(
        spans, node_id, span_cap, generation=generation, _normalized=_normalized)
    # Both production readers (`load_spans` and SpanIndex's full-offset reads) have already crossed
    # `_normalize_span`'s security boundary. Re-running its text redaction/entropy scan over as many
    # as 4096 prompt-heavy rows on every 4 s live poll was pure work (measured seconds at the ceiling).
    # The default remains fail-closed for public/direct callers; only explicit trusted call sites skip.
    spans = list(selected) if _normalized else _normalize_spans(selected)
    stages, matching_spans = _conversation_bands(spans, keep=lambda s, trace_nid: (
        (nid := effective_node_id(s, trace_nid)) is not None and str(nid) == str(node_id)))
    return _conversation_payload(state, stages, matching_spans, observed_total=observed_total,
                                 total_spans=total_spans, span_cap=span_cap,
                                 identity={"node_id": str(node_id)})


def build_trace_conversation(state: RunState, spans: list[dict], trace_id, *, total_spans=None,
                             span_cap: int = TRACE_CONVERSATION_SPAN_CAP,
                             _normalized: bool = False) -> dict:
    """ONE operation's trace as the SAME linear conversation the node surface reads.

    The Researcher's proposal is the case this exists for. Its spans carry no node_id at all
    (measured on `runs/rubertlite-dr-unified-v5` card-0: 252 spans, 86 generations, 165 tool calls,
    every one of them `node_id=None`), so no node conversation can ever contain them — which is why
    a proposal was readable only as a raw span tree, in the card AND in the node's research
    disclosure, with the view switcher inert beside it. Everything about the reading is identical;
    only the SELECTION differs, so only the selection is written here.

    The caller passes the spans it wants read (the index serves one trace by byte offset); this
    filters by `trace_id` anyway, because a caller handing over the whole run must not silently get
    a conversation of every operation in it.
    """
    mine = [s for s in spans if str(s.get("trace_id") or "") == str(trace_id)]
    selected, observed_total = _bounded_tail(mine, span_cap)
    spans = list(selected) if _normalized else _normalize_spans(selected)
    stages, matching_spans = _conversation_bands(spans, keep=lambda s, trace_nid: True)
    return _conversation_payload(state, stages, matching_spans, observed_total=observed_total,
                                 total_spans=total_spans, span_cap=span_cap,
                                 identity={"trace_id": str(trace_id)})


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
    root_nid: dict[str, Optional[int]] = {
        tid: trace_root_node_id(sps, _normalized=True) for tid, sps in by_trace.items()
    }

    node_spans: dict[str, list[dict]] = defaultdict(list)
    unscoped_spans: list[dict] = []
    for s in spans:
        nid = effective_node_id(s, root_nid.get(s.get("trace_id")))
        (node_spans[str(nid)] if nid is not None else unscoped_spans).append(s)
    nodes: dict[str, list[dict]] = {
        nid: _tree(sps, _normalized=True) for nid, sps in node_spans.items()
    }
    unscoped = _tree(unscoped_spans, _normalized=True)

    errors = [s for s in spans if s.get("status") == "ERROR"]
    run_roll = _rollup(spans)
    exporter_loss = _exporter_loss_rollup(spans)
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
            "dropped_spans": exporter_loss["dropped_spans"],
            "export_failures": exporter_loss["export_failures"],
            "exporter_loss_receipts": exporter_loss["loss_receipts"],
            # Loss receipts are deltas.  A bounded tail cannot prove that an older receipt was not
            # omitted, so never present the visible sum as a complete postmortem counter in that case.
            "exporter_metrics_partial": projection["omitted_spans"] > 0,
            "errors": len(errors),
            "generations": run_roll["generations"],
            "tools": run_roll["tools"],
            "tokens": run_roll["tokens"],
            "cost": run_roll["cost"],
            "total_eval_seconds": round(state.total_eval_seconds, 3),
        },
    }


# THE CARD IS THE UNIT OF RESEARCH. A Card is one hypothesis; the Researcher proposes it and the
# Developer builds one or more NODES under it. Before `orchestrator.stamp_proposal_span` there was no
# join between the two halves at all (no span carried a card id, no card event carried a trace id),
# so the two were only ever reachable from different screens — which is what made an operator hunt
# around the UI for work that belongs to one story.
#
# This assembles that story in the order it happened: the proposal(s) first, then each node the card
# produced. Sections are LIGHT rows, not trees: each names its trace so the reader opens only the one
# they want, through `/trace/by_trace/{trace_id}` and `/nodes/{n}/trace`, both already bounded.
def project_card_trace(spans: list[dict], *, card_id: str, node_ids: list,
                       node_trace_ids: Optional[dict] = None,
                       _normalized: bool = False) -> dict:
    """Ordered sections for ONE card: its research, then its nodes.

    `node_ids` and `node_trace_ids` come from the folded event log — the fold is the only place that
    knows which nodes a card owns (`idea.card_id`) and which trace each node's build ran in
    (`node_created.trace_id`). Passing them in keeps this function pure over spans, and keeps the
    ownership question answered by the one component that can answer it.

    RESEARCH is matched two ways, because the engine can only stamp one of them:
      * directly — a `propose` span carrying this `card_id` (the draft/debug/improve paths);
      * by trace — a `propose` span sharing a trace with one of this card's `node_created` events,
        which is how a node reset's RE-proposal is reachable: that path drops the old card and mints
        the replacement after the span closes, so the span cannot name it, but the trace can.
    Never by time or by adjacency: a guessed link would put another hypothesis's reasoning under this
    card, which is worse than showing none.
    """
    spans = spans if _normalized else _normalize_spans(spans)
    owned_traces = {str(tid) for tid in (node_trace_ids or {}).values() if tid}
    wanted_nodes = {str(n) for n in node_ids}

    research: list[dict] = []
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for span in spans:
        by_trace[span.get("trace_id")].append(span)
    for tid, trace_spans in by_trace.items():
        for root in (s for s in trace_spans if not s.get("parent_id") and s.get("name") == "propose"):
            attributes = root.get("attributes") or {}
            stamped = str(attributes.get("card_id") or "")
            if stamped != str(card_id) and str(tid) not in owned_traces:
                continue
            roll = _rollup(trace_spans)
            research.append({
                "name": "propose",
                "trace_id": tid,
                "span_id": root.get("span_id"),
                "start": _finite_number(root.get("start"), nonnegative=True),
                "duration_s": _finite_number(root.get("duration_s"), nonnegative=True),
                "status": root.get("status"),
                # How this row was matched, so the reader is never left guessing which rule applied.
                "link": "card_id" if stamped == str(card_id) else "shared_trace",
                "proposed_for_node": attributes.get("proposed_for_node"),
                "operator": attributes.get("operator"),
                "spans": len(trace_spans),
                "generations": roll["generations"],
                "tools": roll["tools"],
                "tokens": roll["tokens"],
            })
    research.sort(key=lambda row: (row["start"], str(row["trace_id"])))
    total_research = len(research)
    # OLDEST-FIRST truncation, like the node-trace tail: the card's own first proposal is the row
    # that explains it, and dropping the head to show the tail would answer a different question.
    research = research[:TRACE_CARD_RESEARCH_CAP]

    nodes = []
    ordered_nodes = sorted(wanted_nodes, key=lambda value: (len(value), value))
    total_nodes = len(ordered_nodes)
    for node_id in ordered_nodes[:TRACE_CARD_NODE_CAP]:
        # `str(_node_id_of(s) or "")` is the bug this codebase already has a whole test file about:
        # node 0's id is FALSY, so that spelling silently gives node 0 an empty section while every
        # other node renders. Compare the resolved value against None instead.
        node_spans = [s for s in spans
                      if (lambda own: own is not None and str(own) == node_id)(_node_id_of(s))]
        roll = _rollup(node_spans)
        nodes.append({
            "node_id": node_id,
            "trace_id": (node_trace_ids or {}).get(node_id)
            or (node_trace_ids or {}).get(int(node_id) if node_id.isdigit() else node_id),
            "spans": len(node_spans),
            "generations": roll["generations"],
            "tools": roll["tools"],
            "tokens": roll["tokens"],
            "errors": sum(1 for s in node_spans if s.get("status") == "ERROR"),
        })

    # The receipt reports the REAL totals against what is visible, on every axis. Hardcoding
    # `visible == total` would have made the caps above unreportable — a truncated projection
    # indistinguishable from a complete one, which is the thing this receipt exists to prevent.
    return {
        "schema": TRACE_PROJECTION_SCHEMA,
        "card_id": str(card_id),
        "research": research,
        "nodes": nodes,
        "projection": _response_projection(
            total_spans=len(spans), visible_spans=len(spans), light=True,
            total_research=total_research, visible_research=len(research),
            total_nodes=total_nodes, visible_nodes=len(nodes)),
    }
