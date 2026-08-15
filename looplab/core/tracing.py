"""Observability (I14, ADR-17): structured diagnostic tracing that stays files-as-truth by default and bridges
to real OpenTelemetry when the SDK is installed.

Design (see 08-tracing-architecture.md):
- We instrument with one tracer facade (`Tracer.span`). Spans nest via a contextvar stack, so
  the JSONL we always emit (`spans.jsonl`) is a real trace tree (trace_id / span_id /
  parent_id), not a flat log. This is the zero-dependency, offline default.
- When `opentelemetry-api` (+ an SDK/exporter configured via OTEL_* env) is importable, each
  span is ALSO opened as a genuine OpenTelemetry span, so ANY OTLP collector (Jaeger / Tempo /
  Honeycomb / …) receives it with no code change. Without the package the bridge is a no-op.
- Export is ASYNCHRONOUS, so a reader/owner boundary raises a barrier (`Tracer.force_flush`) over
  everything the exporter has accepted — a row already handed to the writer included, not just the
  queued ones. State what it settles precisely: the ONE delegate attempt each accepted row gets, and
  not that attempt's success. A row whose attempt raised is permanently absent from `spans.jsonl` and
  the barrier still returns True, because that boolean reports the LOSS RECEIPT. The absence is never
  silent — the same barrier emits a coalesced `looplab.exporter.loss` row and `export_failures`
  counts it — but a caller who needs "every span I queued is in the artifact" must read those.
- Spans are diagnostics — HARD-SEPARATE from the domain `events.jsonl`. `replay.fold` never
  reads spans, so tracing can be incomplete/non-deterministic without touching engine state.
  Events carry the active (trace_id, span_id) (see eventstore) so the UI can join the research
  tree (from events) to its execution detail (from spans).

Trace topology: top-level operations (create_node / evaluate / ablate / confirm_seed /
onboard) each start a NEW trace (`new_trace=True`) tagged with run_id + node_id, rather than
one giant per-run trace. Real runs are long and resumable; per-operation traces stay bounded
and survive resume, and the run-level tree is reconstructed from events, not a single trace.
"""
from __future__ import annotations

import contextvars
import hashlib
import io
import math
import os
import sys
import threading
import time
import weakref
from collections import deque
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Mapping, Optional

import orjson

from looplab.core.redact import (bounded_redacted_tree, is_secret_key_name,
                                 redact_persisted_identity, redact_persisted_text)
from looplab.core.trace_append import (
    SPAN_APPEND_JOURNAL_MAX_BYTES, SPAN_APPEND_JOURNAL_NAME,
    SPAN_APPEND_RECEIPT_SCHEMA)
from looplab.core.trace_files import (
    TRACE_JSONL_ROW_MAX_BYTES, TRACE_WRITER_LOCK_NAME, open_private_trace_file,
    trace_file_change_token)


_TRACE_TEXT_CAP = 64_000
_TRACE_SPAN_NAME_CAP = 160
_TRACE_SPAN_KIND_CAP = 32
_TRACE_META_CAP = 160
_TRACE_RUN_ID_CAP = 256
_TRACE_ID_CAP = 256
_TRACE_TOOL_CALLS_MAX = 32
_TRACE_TOOL_ARGUMENT_CAP = 16_000
_TRACE_MESSAGES_MAX = 64
_TRACE_TREE_ITEMS_MAX = 64
_TRACE_TREE_DEPTH_MAX = 5
_TRACE_TREE_TOTAL_ITEMS_MAX = 256
# Topology/lifecycle attributes must not compete with arbitrary diagnostic payload for the same
# first-to-last budget.  In particular, ``oversized=...`` appearing before ``node_id``/``generation``
# used to spend the whole character budget and silently erase the indexing fence.  These keys are
# bounded independently below; reserve enough of the aggregate record budget for every one of them.
_TRACE_STRUCTURAL_TEXT_RESERVE = 1_024
_TRACE_STRUCTURAL_ITEM_RESERVE = 64
_TRACE_STRUCTURAL_ATTRIBUTES = (
    "node_id", "generation", "attempt", "phase", "phase_span",
    "input_from", "input_carry", "input_partial", "build_trace",
)
_TRACE_RESERVED_ATTRIBUTE_KEYS = frozenset((*_TRACE_STRUCTURAL_ATTRIBUTES, "looplab.run_id"))
_TRACE_STRUCTURAL_CAPS = {
    "node_id": 128,
    "generation": 64,
    "attempt": 64,
    "phase": _TRACE_SPAN_NAME_CAP,
    "phase_span": 128,
    "input_from": 128,
    "input_carry": 64,
    "input_partial": 32,
    # `build_trace` is the ONE attribution key that points OUT of its own span: the trace id of the
    # run-scoped work that produced this node (`events/traceview.py::claimed_build_traces`). It is
    # structural for the same reason `node_id` is — an oversized diagnostic payload that spent the
    # aggregate budget before reaching it would silently delete a node's whole build from its trace,
    # which is precisely the defect the attribute exists to fix — and reserved for the same reason:
    # arbitrary metadata must not be able to normalize ONTO an attribution key and claim a trace.
    "build_trace": 128,
}

_OTEL_ENV_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})

# The local files-as-truth exporter mirrors OTel's BatchSpanProcessor lifecycle, but keeps both an
# item bound and a byte bound.  TRACE_JSONL_ROW_MAX_BYTES is 8 MiB, so an item-only queue would still
# permit ``capacity * 8 MiB`` resident memory.  The worker stores the already-serialized physical row:
# exact byte accounting and no unbounded deepcopy, at the deliberate cost of keeping JSON encoding on
# the span-closing caller.  File open/heal/lock/flush/receipt I/O is what moves off that caller.
TRACE_EXPORT_QUEUE_MAX_SPANS = 256
TRACE_EXPORT_QUEUE_MAX_BYTES = 16 * 1024 * 1024
TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS = 30_000
_TRACE_EXPORT_WORKER_IDLE_S = 60.0
_TRACE_EXPORT_LOSS_RECEIPT_INTERVAL_S = 1.0

_TRACE_EXPORT_DROP_REASONS = (
    "queue_full",
    "queue_bytes",
    "serialization_error",
    "worker_start",
    "shutdown",
    "shutdown_timeout",
)


def _otel_sdk_disabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether the process explicitly disabled the OTel SDK.

    Keep this independent of the optional OTel packages so the edge configuration contract is
    deterministic and testable in the zero-dependency install too.  The OTel specification uses
    ``true``; accepting the conventional truthy spellings is deliberately operator-friendly.
    """
    source = os.environ if env is None else env
    return str(source.get("OTEL_SDK_DISABLED", "")).strip().lower() in _OTEL_ENV_TRUE


def _otel_env_requests_otlp(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether env explicitly requests LoopLab's built-in OTLP auto-wiring.

    Merely selecting another standard exporter (``console``, ``zipkin``, ``none``, ...) must not
    silently replace it with OTLP.  Either OTLP itself or one of its standard endpoint variables is
    an explicit request.  ``OTEL_SDK_DISABLED`` always wins.
    """
    source = os.environ if env is None else env
    if _otel_sdk_disabled(source):
        return False
    raw_exporters = str(source.get("OTEL_TRACES_EXPORTER", "")).strip()
    exporters = {
        item.strip().lower()
        for item in raw_exporters.split(",")
        if item.strip()
    }
    # An explicit signal exporter selection is authoritative over endpoint variables inherited from
    # a wider deployment environment. In particular, `none` must never install our OTLP provider.
    if raw_exporters:
        return "otlp" in exporters and "none" not in exporters
    return bool(
        str(source.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
        or str(source.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")).strip()
    )


def _trace_text(value, *, cap: int = _TRACE_TEXT_CAP, single_line: bool = False) -> str:
    """Always-safe text for JSONL and mirrored OTel diagnostics."""
    return redact_persisted_text(
        value, max_chars=cap, entropy=True, single_line=single_line)


def _trace_label(value, *, cap: int) -> str:
    """A bounded/redacted metadata label that remains one physical/display line when truncated."""
    # `redact_persisted_text(single_line=True)` collapses input newlines before bounding, but its visible
    # truncation receipt deliberately starts with a newline. Labels have no multiline rendering contract,
    # so flatten that receipt delimiter too; benign, untruncated labels remain byte-identical.
    return _trace_text(value, cap=cap, single_line=True).replace("\n", " ")


def _trace_messages(messages) -> list[dict]:
    """Keep a globally bounded, newest-first-budgeted suffix of one replayed conversation."""
    if not isinstance(messages, (list, tuple)):
        return []
    remaining = _TRACE_TEXT_CAP
    newest: list[dict] = []
    # A tool loop resends its whole history. Retain the most recent turns and spend the aggregate text
    # budget from newest to oldest, rather than allowing every historical message its own 64 KiB cap.
    for raw in reversed(messages[-_TRACE_MESSAGES_MAX:]):
        if remaining <= 0:
            break
        message = raw if isinstance(raw, dict) else {"role": "user", "content": raw}
        role = _trace_text(message.get("role", "user"), cap=min(32, remaining), single_line=True)
        remaining = max(0, remaining - len(role))
        content = _trace_text(_as_text(message.get("content")), cap=remaining)
        remaining = max(0, remaining - len(content))
        newest.append({"role": role, "content": content})
    newest.reverse()
    return newest


def sanitize_trace_value(value, *, max_chars: int = _TRACE_TEXT_CAP,
                         max_items: int = _TRACE_TREE_ITEMS_MAX,
                         max_depth: int = _TRACE_TREE_DEPTH_MAX,
                         max_total_items: int = _TRACE_TREE_TOTAL_ITEMS_MAX):
    """Bound and redact an untrusted structured value while preserving a small JSON-compatible shape.

    The walk itself is `core/redact.py::bounded_redacted_tree`, shared with the advisory-payload
    projection (doc 25 CO-06). A trace value spends its WHOLE remaining character budget on one
    string (`str_cap=None`): a span is already the bounded record, and truncating each field again
    would drop the tail of the one field that mattered.
    """
    return bounded_redacted_tree(
        value, [max(0, int(max_chars))], [max(0, int(max_total_items))],
        max_items=max(0, int(max_items)), max_depth=max(0, int(max_depth)),
        str_cap=None, key_cap=160)


def _sanitize_initial_attributes(attributes: dict) -> dict:
    """Sanitize one span's constructor attributes without starving identity/lifecycle keys.

    The generic payload retains a strict aggregate budget.  Structural values cross the same durable
    sanitizer, but each receives a tiny independent cap and item budget so their presence depends only
    on their own value, never on keyword order or an unrelated prompt-sized diagnostic.  Rebuilding in
    caller order preserves the ordinary JSON shape for benign instrumentation.
    """
    structural = {}
    for key in _TRACE_STRUCTURAL_ATTRIBUTES:
        if key in attributes:
            structural[key] = sanitize_trace_value(
                attributes[key], max_chars=_TRACE_STRUCTURAL_CAPS[key], max_items=8,
                max_depth=2, max_total_items=8)
    generic_raw = {key: value for key, value in attributes.items()
                   if key not in _TRACE_STRUCTURAL_CAPS}
    generic = sanitize_trace_value(
        generic_raw,
        max_chars=_TRACE_TEXT_CAP - _TRACE_STRUCTURAL_TEXT_RESERVE,
        max_total_items=_TRACE_TREE_TOTAL_ITEMS_MAX - _TRACE_STRUCTURAL_ITEM_RESERVE,
    )
    generic = generic if isinstance(generic, dict) else {}
    return {
        key: (structural[key] if key in structural else generic[key])
        for key in attributes
        if key in structural or key in generic
    }


# Active span stack (per async task / thread — contextvars are copied across anyio.to_thread
# and task spawns, so nesting works through the worker-thread eval too).
_stack: contextvars.ContextVar[tuple] = contextvars.ContextVar("LOOPLAB_spans", default=())

# The Tracer whose span is currently open on THIS task/thread. Set by `Tracer.span` for the span's
# lifetime so nested code (the LLM client, the tool loop) can open child observations
# (generations / tools) via the module-level `generation()` / `tool()` helpers WITHOUT threading a
# Tracer reference through every call. contextvar => concurrency-safe: a second run/assistant on
# another task sees its own tracer (or None when nothing is traced). None => the helpers no-op.
_current_tracer: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_tracer", default=None)

# Active node_id for THIS task/thread. Set whenever a span carries an explicit `node_id` (create_node /
# evaluate / repair), and STAMPED onto every nested span that doesn't set its own — so a generation/tool
# span deep inside the Developer's tool-loop is attributable to the node being built, even when the
# tool-loop opens its spans in a long-lived trace of its own (the LLM client / agent don't nest under
# create_node's trace). Without this, per-node trace views come back empty for the very spans a user
# most wants to inspect (the bounded recorded model I/O + tool calls). Copied across task/thread spawns
# like the other contextvars, so it survives the worker-thread eval + anyio.to_thread offloads.
_node_ctx: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_node", default=None)

# Active lifecycle generation for THIS trace context.  The trace root is exported LAST, after every
# child closes, but the span index and live conversation routes must fence those already-durable
# children immediately.  Stamp the root's generation on every descendant, just like ``node_id``.
# A nested ``new_trace`` may establish its own explicit generation and the context token restores the
# outer lifecycle when it closes; a nested trace with no explicit value remains in the node's current
# lifecycle.  Within one trace, the active root value wins because lifecycle is a TRACE property, not
# a per-observation retry label.
_generation_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "LOOPLAB_generation", default=None)

# Active PHASE (nearest enclosing operation span's name: propose / implement / repair / evaluate / …).
# An operation span is written to spans.jsonl only on CLOSE, so while a sub-loop runs its live child
# generation/tool spans reference a parent NOT yet on disk — the live trace view then can't band them
# under their phase and mis-groups them (Developer calls shown under the Researcher until the node
# finishes). Stamping the phase name onto each child span's attributes (like node_id) lets the view read
# the phase from the child itself, no parent lookup needed, so live attribution is correct immediately.
# Copied across task/thread spawns like the other contextvars.
_phase_ctx: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_phase", default=None)

# Prior generation in THIS context, as (span_id, trace_id, full_input_list) — the seam for delta-encoded
# LLM input (see `generation`). The agent tool-loop re-sends the WHOLE growing conversation on every
# turn, so storing each generation's full `input` makes ~90% of spans.jsonl a re-send of the same
# messages. Instead, when a generation STRICTLY EXTENDS the prior one (only appended messages), we store
# just the appended tail + a back-ref, shrinking spans.jsonl ~6x; the trace views reconstruct the full
# input from the chain when a single observation is expanded. Copied across task/thread spawns like the
# other contextvars — but even if a copy is stale, a trace-id mismatch just resets the chain to a full
# base, so correctness never depends on propagation, only compression does.
_prev_gen: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_prev_gen", default=None)


def _init_otel():  # pragma: no cover - exercised only with the [otel] extra installed
    """Return an OpenTelemetry tracer if the SDK is installed, configuring an OTLP exporter
    only when the standard OTEL_* env explicitly selects OTLP (or an OTLP endpoint is set).
    Any problem -> None (bridge off, the JSONL exporter still works). Operators can also use the
    `opentelemetry-instrument` CLI, which configures the global provider itself; we just call
    get_tracer in that case. ``OTEL_SDK_DISABLED`` disables both paths."""
    if _otel_sdk_disabled():
        return None
    try:
        from opentelemetry import trace as _otrace
    except Exception:  # noqa: BLE001 - package absent -> bridge off
        return None
    try:
        # If an endpoint/exporter is configured but no real provider is installed yet, wire a
        # batched OTLP exporter once. (No-op if a provider is already set, e.g. by the CLI.)
        if _otel_env_requests_otlp():
            from opentelemetry.sdk.trace import TracerProvider
            cur = _otrace.get_tracer_provider()
            if not isinstance(cur, TracerProvider):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                provider = TracerProvider(resource=Resource.create({"service.name": "looplab"}))
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                _otrace.set_tracer_provider(provider)
    except Exception:  # noqa: BLE001 - SDK present but mis-wired -> fall through to the check
        pass
    # Only bridge when a REAL (SDK) recording provider is configured. With the API present but
    # no provider, spans are non-recording no-ops — bridging would pay per-span enter/exit +
    # attribute-mirroring for nothing. Return None then, so Tracer.span skips OTel entirely.
    try:
        from opentelemetry.sdk.trace import TracerProvider
        if isinstance(_otrace.get_tracer_provider(), TracerProvider):
            return _otrace.get_tracer("looplab")
    except Exception:  # noqa: BLE001 - sdk not installed -> no real provider possible
        pass
    return None


# Optional OpenTelemetry bridge — a no-op without the package; JSONL export is unaffected.
_OTEL = _init_otel()


def _hex(nbytes: int) -> str:
    return os.urandom(nbytes).hex()


def current_ids() -> tuple[Optional[str], Optional[str]]:
    """(trace_id, span_id) of the active span, or (None, None). The event store stamps these
    into every domain event so events and spans cross-reference (UI join key)."""
    st = _stack.get()
    return (st[-1]["trace_id"], st[-1]["span_id"]) if st else (None, None)


# LLM I/O capture (ADR-17): off by default at import; each run turns it on from its own
# Settings.trace_llm_io. When on, record_llm_call attaches the prompt+completion as a span
# event on whatever operation span is active (propose/implement/repair), so the UI gets a bounded,
# canonicalized and heuristically redacted diagnostic view — without a new transport or event-log change.
#
# The permission is SCOPED, not a bare process flag. Tracers/runs are concurrent inside ONE process
# (the assistant, genesis, a library caller driving two Engines), and with a single global the LAST
# caller of `set_llm_capture` decided for all of them — exposing prompts from an opted-out run, or
# suppressing an opted-in one. A Tracer may therefore declare its OWN policy
# (`Tracer(..., capture_llm_io=…)`, wired from that run's `Settings.trace_llm_io`), which `Tracer.span`
# binds to `_capture_ctx` for the span's lifetime. This module-global remains the PROCESS-WIDE DEFAULT
# for tracers that declare nothing (a bare `Tracer(...)`, the CLI passthrough, the seam tests).
_CAPTURE_LLM_IO = False

# Effective capture policy for the span open on THIS task/thread: True/False when the active Tracer
# declared one, None => defer to the process-wide default above. Set/reset by `Tracer.span` alongside
# the node/phase tokens, and copied across task/thread spawns like the other contextvars — so the
# policy follows its run into the `anyio.to_thread` eval workers and the concurrent build threads.
_capture_ctx: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_capture", default=None)


def set_llm_capture(enabled: bool) -> None:
    """Set the PROCESS-WIDE default capture policy. A Tracer that declares `capture_llm_io` overrides
    this inside its own spans; everything untraced (or traced by an undeclared Tracer) follows it."""
    global _CAPTURE_LLM_IO
    _CAPTURE_LLM_IO = bool(enabled)


def llm_capture_enabled() -> bool:
    """Is LLM I/O capture permitted RIGHT HERE? The active span's scoped policy wins; with no scoped
    policy (nothing traced, or a Tracer that declared none) the process-wide default applies."""
    scoped = _capture_ctx.get()
    return _CAPTURE_LLM_IO if scoped is None else scoped


def record_llm_call(*, op: str, model: str, messages: list[dict], completion: str,
                    thinking: Optional[str] = None, usage: Optional[dict] = None) -> None:
    """Append an `llm_call` event to the active span record (no-op when capture is off or there
    is no active span — e.g. an LLM call outside any traced operation). Persisted text is bounded,
    canonicalized and heuristically redacted here; it is a diagnostic representation, not an exact transcript.

    `completion` is the model's clean answer (the conclusion the UI surfaces). `thinking`, when
    present, is the provider-returned reasoning after the same sanitizer — stored separately so the UI can keep
    it as a collapsed debug-only disclosure rather than the primary view."""
    if not llm_capture_enabled():
        return
    st = _stack.get()
    if not st:
        return
    rec = st[-1]
    # `op` and `model` are durable metadata too.  Unlike the prompt/completion paths below they used
    # to bypass the persistence sanitizer entirely, so a provider/wrapper supplied value could retain
    # credentials, controls or an unbounded string in both spans.jsonl and OTLP.  Keep ordinary labels
    # byte-identical while applying the same single-line metadata boundary as span names.
    safe_op = _trace_label(op, cap=_TRACE_META_CAP)
    safe_model = _trace_label(model, cap=_TRACE_META_CAP)
    # Coerced, never raw `int()`: a provider that reports {"prompt_tokens": "n/a"} would otherwise
    # raise out of the TRACER and take the traced operation with it, which is exactly what this
    # module promises never to do. Today's callers hand over ints already sanitized by
    # `_safe_token_count`, so this is the guard for the next caller, not a live fix.
    tokens = {
        "prompt": _token_int((usage or {}).get("prompt_tokens")),
        "completion": _token_int((usage or {}).get("completion_tokens")),
        "total": _token_int((usage or {}).get("total_tokens")),
    }
    ev = {
        "name": "llm_call",
        "op": safe_op,
        "model": safe_model,
        "prompt": _trace_messages(messages),
        "completion": _trace_text(completion or ""),
        "tokens": tokens,
    }
    if thinking:
        ev["thinking"] = _trace_text(thinking)
    rec["events"].append(ev)
    # Mirror to the active OpenTelemetry span when the bridge is live, so OTLP collectors
    # (Jaeger/Tempo/…) see LLM I/O too — not just spans.jsonl. Best-effort; primitives only.
    active_tracer = _current_tracer.get()
    if active_tracer is not None and active_tracer._otel is not None:
        try:
            from opentelemetry import trace as _ot
            _ot.get_current_span().add_event("llm_call", {
                "op": safe_op, "model": safe_model,
                "prompt_tokens": tokens["prompt"], "completion_tokens": tokens["completion"],
                "total_tokens": tokens["total"],
                "completion": _trace_text(completion or "", cap=2000),
            })
        except Exception:  # noqa: BLE001 - mirroring must never affect the run
            pass


def _as_text(content) -> str:
    """OpenAI content can be a string or a list of content parts; normalize to text for display."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _token_int(value) -> int:
    """A token count as a plain non-negative int, never a raise. Tracing must not be able to perturb
    the operation it observes, and a provider is free to report "n/a" (or None, or a float) where the
    schema says a number — a bare `int()` on that would take the traced call down with it."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _norm_usage(tokens) -> dict:
    """Accept either OpenAI usage ({prompt_tokens,…}) or our short form ({prompt,…})."""
    t = tokens or {}
    p = _token_int(t.get("prompt_tokens") or t.get("prompt"))
    c = _token_int(t.get("completion_tokens") or t.get("completion"))
    return {"prompt": p, "completion": c,
            "total": _token_int(t.get("total_tokens") or t.get("total") or (p + c))}


def _redacted_error(value) -> str:
    """Bounded durable error text; tracing must never persist credentials from provider failures."""
    return _trace_text(value, cap=500)


class ObservationHandle:
    """Fluent handle for a first-class observation (a `generation` = one LLM call, or a `tool` = one
    tool invocation) — the Langfuse-style tree node. Wraps a SpanHandle (or None when untraced) and
    lets the caller attach the OUTPUT / token usage / cost / reasoning AFTER the call returns, so the
    span carries a bounded diagnostic input→output record with real latency (the enclosing span's duration)."""

    def __init__(self, h: "SpanHandle | None"):
        self._h = h
        # Bind the capture policy AT CONSTRUCTION — inside the owning span, where `_capture_ctx` still
        # holds THIS run's decision. The caller attaches output/thinking/tool_calls later, and for a
        # streamed generation that happens across suspensions of a generator whose consumer may have
        # entered another run's span in between (a sync generator body runs in the CALLER's context,
        # so the contextvar there is the consumer's, not ours). Re-reading it per attach would apply
        # the wrong run's policy to this observation.
        self._capture = llm_capture_enabled()

    @property
    def active(self) -> bool:
        return self._h is not None

    def set(self, key: str, value) -> "ObservationHandle":
        if key == "tool_calls":
            return self.tool_calls(value)
        if self._h is not None:
            self._h.set(key, value)
        return self

    def output(self, text) -> "ObservationHandle":
        if self._h is not None and self._capture and text is not None:
            self._h.set("output", _trace_text(text if isinstance(text, str) else _as_text(text)))
        return self

    def usage(self, tokens) -> "ObservationHandle":
        if self._h is not None and tokens:
            self._h.set("usage", _norm_usage(tokens))
        return self

    def cost(self, c) -> "ObservationHandle":
        if self._h is not None and c is not None:
            try:
                value = float(c)
                if math.isfinite(value):
                    self._h.set("cost", value)
            except (TypeError, ValueError, OverflowError):
                pass
        return self

    def thinking(self, t) -> "ObservationHandle":
        if self._h is not None and self._capture and t:
            self._h.set("thinking", _trace_text(t))
        return self

    def tool_calls(self, calls) -> "ObservationHandle":
        """Attach a bounded, redacted preview of model-requested tools when I/O capture is enabled.

        Tool arguments are model output and commonly contain file bodies, URLs, tokens, or credentials
        echoed from context. They therefore follow the same opt-in and durable-redaction boundary as prompt
        and completion text instead of going through the generic attribute setter.
        """
        if self._h is None or not self._capture or not isinstance(calls, (list, tuple)):
            return self
        safe = []
        remaining = _TRACE_TEXT_CAP
        for call in islice(calls, _TRACE_TOOL_CALLS_MAX):
            if not isinstance(call, dict):
                continue
            name = _trace_text(call.get("name"), cap=min(128, remaining), single_line=True)
            remaining = max(0, remaining - len(name))
            arguments = _trace_text(
                call.get("arguments"), cap=min(_TRACE_TOOL_ARGUMENT_CAP, remaining))
            remaining = max(0, remaining - len(arguments))
            safe.append({"name": name, "arguments": arguments})
            if remaining <= 0:
                break
        if safe:
            self._h.set("tool_calls", safe)
        return self

    def error(self, msg: str) -> "ObservationHandle":
        if self._h is not None:
            self._h.set("level", "ERROR").event("exception", error=_redacted_error(msg))
        return self


_NULL_OBS = ObservationHandle(None)


@contextmanager
def generation(*, op: str, model: str, messages: Optional[list] = None,
               model_parameters: Optional[dict] = None):
    """Open a first-class GENERATION observation (one LLM call) as a child of the active span — the
    Langfuse `generation` node. Nests under whatever operation span is live (propose/implement/repair,
    or a tool-loop iteration). Records the input messages up front; the caller attaches output/usage/
    cost/thinking on the yielded handle. No-op (yields a null handle) when nothing is being traced."""
    tr = _current_tracer.get()
    if tr is None:
        yield _NULL_OBS
        return
    attrs = {"op": op, "model": model}
    if model_parameters:
        attrs["model_parameters"] = sanitize_trace_value(model_parameters)
    with tr.span("generation", kind="generation", **attrs) as h:
        if messages is not None and llm_capture_enabled():
            # Generation input replays tool observations on the next turn. Sanitize the
            # whole conversation here, not only the dedicated tool span, before delta encoding/OTel.
            cur = _trace_messages(messages)
            # Delta-encode the re-sent history: when this generation STRICTLY EXTENDS the prior one IN
            # THIS TRACE (only appended to it), store just the appended tail, plus a back-ref
            # (`input_from`) + carried-prefix count (`input_carry`). A fresh trace / sub-loop whose
            # history diverges just stores a full base (input_from=None). ~6x smaller spans.jsonl;
            # `/spans/{sid}` and `/trace/by_trace`
            # reconstruct the full retained diagnostic input from the chain (traceview.hydrate_inputs). The
            # reader tolerates old logs (no input_carry ⇒ the `input` IS the complete retained list).
            prev = _prev_gen.get()
            tid, sid = h._rec.get("trace_id"), h._rec.get("span_id")
            # Chain ONLY when this generation STRICTLY EXTENDS the prior one in the same trace (a
            # tool-loop turn that appended messages to the SAME conversation — the prior input is a full
            # prefix of this one). A new trace, or a CONTEXT RESET / new sub-loop (propose→implement→
            # repair: the history shrank or diverged, so it is NOT a strict extension) stores a full
            # base (input_from=None). This keeps every sub-loop start self-contained: the conversation
            # view (`_thread_turns`) treats `input_carry == 0` as the request boundary and shows its full
            # request, and reconstruction never has to cross a reset. Base inputs are the small initial
            # context (system+user), so compression is unaffected — the grown history stays delta'd.
            # Require np > 0: a zero-length carry saves nothing and would leave a dangling `input_from`
            # with carry=0, so `input_from is not None` on disk always implies a real carried prefix.
            np = len(prev[2]) if prev is not None else 0
            if prev is not None and prev[1] == tid and np > 0 and len(cur) >= np and cur[:np] == prev[2]:
                h.set_many(input=cur[np:], input_carry=np, input_from=prev[0])
            else:
                h.set_many(input=cur, input_carry=0, input_from=None)
            _prev_gen.set((sid, tid, cur))
        yield ObservationHandle(h)


@contextmanager
def tool(name: str, arguments=None):
    """Open a first-class TOOL observation (one tool invocation) as a child of the active span — the
    Langfuse `tool` node. Records the call arguments up front; the caller attaches the result via
    `.output(...)` (and `.error(...)` on failure). No-op when nothing is being traced."""
    tr = _current_tracer.get()
    if tr is None:
        yield _NULL_OBS
        return
    safe_name = _trace_text(name, cap=128, single_line=True)
    with tr.span("tool", kind="tool", tool=safe_name) as h:
        if arguments is not None and llm_capture_enabled():
            h.set("input", sanitize_trace_value(arguments))
        yield ObservationHandle(h)


@contextmanager
def operation(name: str, **attributes):
    """Open a first-class OPERATION span (a phase / sub-loop — propose / stages / plan / implement /
    repair) as a child of the active span, on the CURRENT run's tracer, so a role (e.g. the Developer)
    can delimit its own phases in the trace WITHOUT holding a Tracer reference. Inherits node_id and
    stamps the phase onto child generations/tools like any operation span. No-op (still runs the body,
    yields None) when nothing is being traced — unit tests / the toy backend keep working."""
    tr = _current_tracer.get()
    if tr is None:
        yield None
        return
    with tr.span(name, kind="operation", **attributes):
        yield


_EXPORT_LOCKS_GUARD = threading.Lock()
# Long-lived servers may open thousands of runs.  Exporter instances hold their lock strongly while
# active; once the last exporter for a path goes away, the weak registry can shed the entry.
_EXPORT_LOCKS: "weakref.WeakValueDictionary[str, object]" = weakref.WeakValueDictionary()
_EXPORT_LOCKS_PID = os.getpid()
# Every exporter-owned descriptor (writer fence, source, append receipt) is registered for the short
# duration it is open. POSIX fork duplicates descriptors from ALL threads; without closing a worker's
# inherited flock fd in the child, the vanished worker can leave the child locking itself forever.
_EXPORT_OPEN_FDS_GUARD = threading.Lock()
_EXPORT_OPEN_STREAMS: dict[int, object] = {}
# Fallback-only child references for opaque custom streams whose raw layer could not be closed
# directly. Their inherited fd is replaced by a /dev/null tombstone and the object is retained, so
# a delayed destructor can neither append copied bytes nor close a subsequently reused fd number.
_EXPORT_FORK_QUARANTINE: list[object] = []


def _reset_export_locks_after_fork() -> None:
    """Drop locks inherited from vanished threads in a fork child.

    POSIX copies a locked ``RLock`` into the child without copying the owning thread.  Reusing an
    exporter after a multi-threaded fork would therefore wait forever.  The child gets a fresh
    registry/guard, and each inherited exporter notices the PID change before its next write.
    """
    global _EXPORT_LOCKS_GUARD, _EXPORT_LOCKS, _EXPORT_LOCKS_PID
    _EXPORT_LOCKS_GUARD = threading.Lock()
    _EXPORT_LOCKS = weakref.WeakValueDictionary()
    _EXPORT_LOCKS_PID = os.getpid()


def _before_export_fork() -> None:
    # Serialize fork with the short descriptor open/register and unregister/close transitions. The
    # guard is never held across healing, append, flush, receipt generation or advisory-lock waits.
    _EXPORT_OPEN_FDS_GUARD.acquire()


def _after_export_fork_parent() -> None:
    _EXPORT_OPEN_FDS_GUARD.release()


def _after_export_fork_child() -> None:
    global _EXPORT_OPEN_FDS_GUARD, _EXPORT_OPEN_STREAMS, _EXPORT_FORK_QUARANTINE
    # These descriptors belonged to exporter calls in the pre-fork process. No such worker survives
    # in the child. Never call BufferedRandom.close(): it flushes bytes copied between write() and
    # flush() and can double-commit the parent's row. Close the unbuffered raw layer instead.
    for fd, stream in _EXPORT_OPEN_STREAMS.items():
        try:
            raw = getattr(stream, "raw", None)
            if raw is None:
                buffer = getattr(stream, "buffer", None)
                raw = getattr(buffer, "raw", buffer)
        except Exception:  # noqa: BLE001 - opaque test/storage wrappers take the fd fallback
            raw = None
        closed = False
        # ``RawIOBase`` alone is not proof that ``close`` retires this OS descriptor without
        # reaching a copied buffered owner: storage/test proxies may expose an arbitrary ``raw``
        # object whose close delegates straight back to ``BufferedRandom.close``. Even a FileIO
        # subclass is unsafe because IOBase.close dynamically dispatches its overridable flush.
        # Only the exact built-in FileIO type is proof of the unbuffered close path. Verify it owns
        # the descriptor registered before fork; otherwise tombstone that known fd below without
        # invoking any proxy method.
        if type(raw) is io.FileIO:
            try:
                if io.FileIO.fileno(raw) == fd:
                    io.FileIO.close(raw)
                    closed = True
            except Exception:  # noqa: BLE001 - fall back to raw fd retirement below
                pass
        if not closed:
            _EXPORT_FORK_QUARANTINE.append(stream)
            try:
                # Closing fd outright would make its number reusable while ``stream`` still believes
                # it owns that number. A delayed proxy/finalizer could then flush or close an unrelated
                # child descriptor. Replacing the inherited source with /dev/null releases its flock
                # and open-file description while keeping the number occupied until the quarantined
                # wrapper eventually closes it.
                devnull_fd = os.open(
                    os.devnull, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
                try:
                    os.dup2(devnull_fd, fd, inheritable=False)
                finally:
                    os.close(devnull_fd)
            except Exception:  # noqa: BLE001 - child fork recovery must finish resetting all locks
                # Last-resort quiescence if /dev/null is unavailable. The quarantine still prevents
                # ordinary collection during the child lifetime; preserve fork progress over a lock.
                try:
                    os.close(fd)
                except Exception:
                    pass
    _EXPORT_OPEN_STREAMS = {}
    _EXPORT_OPEN_FDS_GUARD = threading.Lock()
    _reset_export_locks_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_export_fork,
        after_in_parent=_after_export_fork_parent,
        after_in_child=_after_export_fork_child,
    )


def _canonical_export_path(path: str | os.PathLike) -> str:
    """Canonical process-local identity for one export destination."""
    # Do NOT realpath the final component: spans.jsonl is required to be a run-local regular file,
    # never a symlink capability to an external/cross-run destination. normpath still makes the
    # common ``alias/../spans.jsonl`` spelling share a lock without following anything.
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fsdecode(os.fspath(path)))))


def _export_lock(path: str | os.PathLike):
    # Defensive fallback for embedders/platform shims that fork without running registered hooks.
    if os.getpid() != _EXPORT_LOCKS_PID:
        _reset_export_locks_after_fork()
    key = _canonical_export_path(path)
    with _EXPORT_LOCKS_GUARD:
        lock = _EXPORT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _EXPORT_LOCKS[key] = lock
        return lock


@contextmanager
def _interprocess_export_guard(stream, *, required: bool = False):
    """Advisory serialization for cooperating exporters in different processes.

    The process-local canonical-path lock is always authoritative for threads/instances here.  This
    additional standard-library guard closes the truncate/append race between two engine processes
    without a sidecar dependency. Data-file/custom-exporter locking stays best-effort for historical
    compatibility. The Engine lifecycle sidecar passes ``required=True`` and fails before opening the
    source if its destructive-write fence cannot be proved. Every exporter acquires locks in the one
    order (path RLock, then lifecycle sidecar, then data file), so there is no reverse-order path.
    """
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        locked = True
    except (OSError, ImportError, AttributeError, NotImplementedError, ValueError) as exc:
        if required:
            raise OSError("required trace lifecycle writer lock is unavailable") from exc
    try:
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError, AttributeError, NotImplementedError, ValueError):
                pass


@contextmanager
def _open_export_append(path: Path):
    """Use the shared descriptor-first boundary for source and receipt appends.

    Passing this module's ``open`` preserves the established monkeypatch/storage-wrapper seam while
    the shared helper supplies no-follow, non-blocking special-file rejection and the post-yield
    pathname/descriptor validation.  That final validation matters to the exporter: a destination
    replaced while bytes are buffered must be reported as unavailable, never accepted as a write to
    the current run-root name.
    """
    manager = open_private_trace_file(path, "a+b", open_file=open)
    with _EXPORT_OPEN_FDS_GUARD:
        stream = manager.__enter__()
        fd = stream.fileno()
        _EXPORT_OPEN_STREAMS[fd] = stream
    try:
        yield stream
    except BaseException:
        exc_info = sys.exc_info()
        with _EXPORT_OPEN_FDS_GUARD:
            _EXPORT_OPEN_STREAMS.pop(fd, None)
            suppress = manager.__exit__(*exc_info)
        if not suppress:
            raise
    else:
        with _EXPORT_OPEN_FDS_GUARD:
            _EXPORT_OPEN_STREAMS.pop(fd, None)
            manager.__exit__(None, None, None)


@contextmanager
def _trace_writer_guard(path: Path, *, enabled: bool):
    """Serialize canonical source appends with destructive reset/clear/archive.

    Those paths own ``TRACE_WRITER_LOCK_NAME`` through ``span_destructive_write_guard``. Using the
    same OS advisory byte/inode here closes the async late-writer race without importing the events
    layer downward into core. Normal index refresh deliberately uses its own lock, so a cold rebuild
    cannot fill this exporter's bounded queue. The enabled lifecycle sidecar is required and fails
    before source open when lock capability/ownership cannot be proved; only the later data-file flock
    retains the historical diagnostic best-effort behavior.

    The lifecycle fence is an explicit Engine-owned capability, not a basename heuristic. Preserve
    embedders that happen to call a custom target ``spans.jsonl``: a default exporter retains the
    data-file flock and does not acquire/create a new sibling sidecar.
    """
    if not enabled:
        yield
        return
    lock_path = path.with_name(TRACE_WRITER_LOCK_NAME)
    with _open_export_append(lock_path) as stream, _interprocess_export_guard(
            stream, required=True):
        yield


def _heal_torn_jsonl_tail(stream) -> int:
    """Discard only the non-newline crash suffix and return the committed EOF."""
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    if end:
        stream.seek(end - 1)
        if stream.read(1) != b"\n":
            pos, newline_at = end, -1
            while pos > 0:
                start = max(0, pos - 65_536)
                stream.seek(start)
                buf = stream.read(pos - start)
                idx = buf.rfind(b"\n")
                if idx != -1:
                    newline_at = start + idx
                    break
                pos = start
            end = newline_at + 1 if newline_at != -1 else 0
            stream.truncate(end)
    stream.seek(0, os.SEEK_END)
    return end


def _last_receipt_identity(stream, end: int) -> Optional[tuple[int, int]]:
    """Best-effort source identity from the final committed receipt."""
    if end <= 0:
        return None
    start = max(0, end - 4096)
    stream.seek(start)
    tail = stream.read(end - start).rstrip(b"\n")
    raw = tail.rsplit(b"\n", 1)[-1]
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    dev, ino = value.get("dev"), value.get("ino")
    if (not isinstance(dev, int) or isinstance(dev, bool)
            or not isinstance(ino, int) or isinstance(ino, bool)):
        return None
    return dev, ino


def _write_append_receipt(source_path: Path, receipt: dict) -> None:
    """Append/rotate the bounded derived receipt journal; failure simply disables fast top-up."""
    journal = source_path.with_name(SPAN_APPEND_JOURNAL_NAME)
    line = orjson.dumps(receipt) + b"\n"
    try:
        with _open_export_append(journal) as stream:
            end = _heal_torn_jsonl_tail(stream)
            identity = (receipt["dev"], receipt["ino"])
            rotate = (end + len(line) > SPAN_APPEND_JOURNAL_MAX_BYTES
                      or (_last_receipt_identity(stream, end) not in (None, identity)))
            if rotate:
                # Source replacement (reset/clear) rotates automatically by inode. In-place
                # compaction also bounds retention; readers whose old cursor exceeds the new size
                # fail closed to one rebuild before checkpointing this journal again.
                stream.seek(0)
                stream.truncate(0)
            stream.seek(0, os.SEEK_END)
            stream.write(line)
            stream.flush()
    except OSError:
        pass


def _json_encoding_fits(value, budget: list[int], *, depth: int = 0) -> bool:
    """Conservatively prove that orjson's encoding fits without serializing the whole value.

    The exporter normally serializes a small span exactly as before. A span can nevertheless grow
    without bound through repeated ``set``/``event`` calls; asking ``orjson.dumps`` whether that
    multi-GB object fits first creates the very allocation the row boundary exists to prevent. This
    walk charges a strict upper bound for the JSON-native shapes our tracer emits and stops as soon
    as the physical-row budget is exhausted. Unknown/custom values fail the proof and go directly to
    the bounded fallback instead of invoking an untrusted ``default=str`` conversion.
    """
    if depth > 64:
        return False

    def take(amount: int) -> bool:
        if amount < 0 or amount > budget[0]:
            return False
        budget[0] -= amount
        return True

    if value is None:
        return take(4)
    if type(value) is bool:
        return take(5)                 # ``false`` is the longer spelling
    if type(value) is str:
        # Quotes/control escapes/UTF-8: six bytes per code point is a safe JSON upper bound.
        length = len(value)
        return length <= budget[0] // 6 and take(2 + 6 * length)
    if type(value) is int:
        # orjson's native integer contract is 64-bit; keep the bound allocation-free.
        return -(1 << 63) <= value <= (1 << 64) - 1 and take(21)
    if type(value) is float:
        return math.isfinite(value) and take(32)
    if type(value) is dict:
        count = len(value)
        if not take(2 + 2 * count):    # braces + conservative comma/colon charge
            return False
        for key, child in value.items():
            if type(key) is not str:
                return False
            key_length = len(key)
            if key_length > budget[0] // 6 or not take(2 + 6 * key_length):
                return False
            if not _json_encoding_fits(child, budget, depth=depth + 1):
                return False
        return True
    if type(value) in {list, tuple}:
        if not take(2 + len(value)):   # brackets + at least one comma charge per item
            return False
        return all(_json_encoding_fits(child, budget, depth=depth + 1) for child in value)
    return False


def _fallback_identifier(value, *, cap: int) -> Optional[str | int]:
    """A bounded identity scalar that never calls an opaque object's string conversion."""
    if type(value) is str:
        if len(value) > cap:
            return None
        return redact_persisted_identity(value, max_chars=cap)
    if type(value) is int and -(1 << 63) <= value <= (1 << 63) - 1:
        return value
    return None


def _fallback_label(value, *, cap: int, default: str) -> str:
    if type(value) is str:
        if len(value) > cap:
            return default
        return _trace_label(value, cap=cap) or default
    if type(value) in {int, float, bool}:
        return _trace_label(value, cap=cap) or default
    return default


def _fallback_timing(value) -> Optional[int | float]:
    if type(value) is int and -(1 << 63) <= value <= (1 << 63) - 1:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _bounded_span_fallback(span) -> dict:
    """Topology-preserving receipt used when a full durable span cannot fit one physical row."""
    source = span if type(span) is dict else {}
    raw_attributes = source.get("attributes")
    raw_attributes = raw_attributes if type(raw_attributes) is dict else {}
    structural = {}
    for key in _TRACE_STRUCTURAL_ATTRIBUTES:
        if key not in raw_attributes:
            continue
        value = raw_attributes[key]
        if type(value) not in {str, int, float, bool, type(None)}:
            continue
        # Do not route an already-over-limit identity/lifecycle string through redaction or the
        # generic tree normalizer: even truncation can scan/copy the entire hostile value first.
        if type(value) is str and len(value) > _TRACE_STRUCTURAL_CAPS[key]:
            continue
        structural[key] = value
    attributes = _sanitize_initial_attributes(structural)
    if (source.get("kind") == "generation"
            or "input_carry" in raw_attributes or "input_from" in raw_attributes):
        # The fallback necessarily omits captured input. Keep its delta-chain identity for later
        # generations, but make the missing ancestor explicit; hydrate_inputs propagates this flag
        # through every descendant instead of presenting their retained suffix as a complete prompt.
        attributes["input_partial"] = True
    attributes.update({
        "looplab.trace_payload_truncated": True,
        "looplab.trace_payload_limit_bytes": TRACE_JSONL_ROW_MAX_BYTES,
    })
    raw_events = source.get("events")
    raw_events_count = len(raw_events) if type(raw_events) in {list, tuple} else 0
    omitted_attributes = max(0, len(raw_attributes) - len(structural))
    fallback = {
        "name": _fallback_label(
            source.get("name"), cap=_TRACE_SPAN_NAME_CAP, default="span"),
        "kind": _fallback_label(
            source.get("kind"), cap=_TRACE_SPAN_KIND_CAP, default="operation"),
        "trace_id": _fallback_identifier(source.get("trace_id"), cap=_TRACE_ID_CAP),
        "span_id": _fallback_identifier(source.get("span_id"), cap=_TRACE_ID_CAP),
        "parent_id": _fallback_identifier(source.get("parent_id"), cap=_TRACE_ID_CAP),
        "run_id": _fallback_identifier(source.get("run_id"), cap=_TRACE_RUN_ID_CAP),
        "attributes": attributes,
        "events": [{
            "name": "looplab.span_payload_truncated",
            "reason": "physical_row_limit",
            "limit_bytes": TRACE_JSONL_ROW_MAX_BYTES,
            "omitted_attributes": omitted_attributes,
            "omitted_events": raw_events_count,
        }],
        "status": _fallback_label(source.get("status"), cap=_TRACE_META_CAP, default="UNSET"),
    }
    for key in ("start", "end", "duration_s"):
        timing = _fallback_timing(source.get(key))
        if timing is not None:
            fallback[key] = timing
    return fallback


def _span_jsonl_line(span) -> bytes:
    """Return one writer-valid physical row, falling back before an unbounded serialization."""
    payload_budget = [TRACE_JSONL_ROW_MAX_BYTES - 1]  # the committing LF consumes the final byte
    if _json_encoding_fits(span, payload_budget):
        try:
            line = orjson.dumps(span, default=str) + b"\n"
        except (TypeError, ValueError, OverflowError):
            line = b""
        if line and len(line) <= TRACE_JSONL_ROW_MAX_BYTES:
            return line

    fallback = _bounded_span_fallback(span)
    line = orjson.dumps(fallback) + b"\n"
    if len(line) > TRACE_JSONL_ROW_MAX_BYTES:  # defensive invariant; fallback is intentionally tiny
        raise ValueError("bounded trace fallback exceeds the physical row contract")
    return line


class _AsyncExportAbandoned(Exception):
    """An async row lost the pre-commit race to a timed-out terminal shutdown."""


class JsonlSpanExporter:
    """Default exporter: one JSON span per line in `spans.jsonl` (files-as-truth, offline)."""

    def __init__(
        self, path: str | os.PathLike, *, lifecycle_fence: bool = False,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Only Engine-owned canonical traces participate in reset/clear/archive fencing. Defaulting
        # off preserves custom embedders, including ones whose unrelated file is named spans.jsonl.
        self._lifecycle_fence = bool(lifecycle_fence)
        # The lock belongs to the canonical destination, not this Python object.  A run can construct
        # more than one Tracer/Exporter (resume, embedding, fan-out); with per-instance locks one healer
        # could inspect a torn EOF, another append a committed row, then the first truncate that row.
        self._lock = _export_lock(self.path)
        self._lock_pid = os.getpid()

    def export(self, span: dict) -> None:
        # Small JSON-native spans stay byte-for-byte identical. Repeated generic instrumentation can
        # otherwise make one row arbitrarily large; preflight it allocation-free and commit a small
        # topology-preserving truncation receipt rather than poisoning this and every later record.
        self._export_line(_span_jsonl_line(span))

    def _export_line(self, line: bytes, *, pre_commit=None) -> None:
        """Commit one row prepared by this module's bounded serializer.

        This private seam lets ``AsyncJsonlSpanExporter`` serialize once on submit for exact byte
        accounting, then move only the file I/O to its worker.  It is deliberately not a generic raw
        append API: callers cannot use it to bypass the one-row/newline/physical-size contract.
        """
        if (type(line) is not bytes or not line.endswith(b"\n")
                or b"\n" in line[:-1] or len(line) > TRACE_JSONL_ROW_MAX_BYTES):
            raise ValueError("prepared trace row violates the physical JSONL contract")
        # An exporter object can survive ``fork()``.  Never acquire the inherited process-local lock:
        # its owner may have been another thread that does not exist in the child.
        pid = os.getpid()
        if pid != self._lock_pid:
            self._lock = _export_lock(self.path)
            self._lock_pid = pid
        # Serialize writes: child spans export from to_thread worker threads while the parent
        # span exports from the event-loop thread; without the lock concurrent appends from
        # distinct handles can interleave into a corrupt JSON line (eval_parallel fan-out).
        with self._lock, _trace_writer_guard(
                self.path, enabled=self._lifecycle_fence):
            # A timed-out async owner may have released engine.lock while this row was waiting for
            # the shared trace-writer guard. Recheck only AFTER that guard is ours: if clear/reset
            # won it first, abandoning here prevents an attempt-A append behind its rewrite. If the
            # row passed this point first, the destructive writer waits for the guard and rewrites
            # after our append, so there is still no late data in the published generation.
            if pre_commit is not None and not pre_commit():
                raise _AsyncExportAbandoned()
            self._export_line_guarded(line)

    def _export_line_guarded(self, line: bytes) -> None:
        """Commit while the canonical path lock and cross-plane writer guard are both held."""
        with _open_export_append(self.path) as f, _interprocess_export_guard(f):
            # A killed writer can leave the final JSON record without its committing newline.  Every
            # forward reader correctly stops before that torn suffix, but appending blindly would glue
            # this record to it and turn the pair into one COMPLETE corrupt line; that line would then
            # hide this and every later span forever.  A newline is the record commit boundary, so only
            # the uncommitted suffix is discarded.  Inspect, heal and append through one descriptor
            # while holding the exporter's writer lock, leaving every committed byte untouched.
            before_size = _heal_torn_jsonl_tail(f)
            before_stat = os.fstat(f.fileno())
            # Read the mutation token through the SAME descriptor that is about to be appended to, so
            # the proof and the bytes cannot belong to two generations of the path. `None` here (an
            # unsupported filesystem or an API failure) keeps the receipt honest: the reader then has
            # no token to check and falls back to rebuilding rather than trusting a blank.
            before_change_token = trace_file_change_token(f.fileno(), before_stat)
            # An update stream must be repositioned between reading/truncating and writing.  O_APPEND
            # also pins the write to the healed EOF, but the seek keeps the Python buffering contract
            # explicit and portable.
            f.seek(0, os.SEEK_END)
            f.write(line)
            # Release the cross-process guard only after the buffered bytes reach the descriptor.
            # This is a visibility/serialization boundary, not an fsync durability promise.
            f.flush()
            after_stat = os.fstat(f.fileno())
            _write_append_receipt(self.path, {
                "schema": SPAN_APPEND_RECEIPT_SCHEMA,
                "dev": after_stat.st_dev,
                "ino": after_stat.st_ino,
                "before_size": before_size,
                "before_mtime_ns": before_stat.st_mtime_ns,
                "before_ctime_ns": before_stat.st_ctime_ns,
                "before_change_token": before_change_token,
                "after_size": after_stat.st_size,
                "after_mtime_ns": after_stat.st_mtime_ns,
                "after_ctime_ns": after_stat.st_ctime_ns,
                "after_change_token": trace_file_change_token(f.fileno(), after_stat),
                "append_sha256": hashlib.sha256(line).hexdigest(),
            })


class AsyncJsonlSpanExporter:
    """Bounded non-blocking queue in front of :class:`JsonlSpanExporter`.

    ``export`` is the OTel ``on_end``-shaped path: it never waits for filesystem I/O and returns a
    boolean acceptance receipt.  Backpressure is explicit drop-newest, preserving FIFO and at-most-one
    delegate attempt for every accepted row.  Drops and delegate failures are never recursive queue
    items: one coalesced diagnostic metric receipt bypasses the ordinary queue on the worker. It also
    gets one attempt: an exception after append is ambiguous, so retrying its delta could over-count
    loss in every postmortem summary. A missing receipt is preferable to false loss inflation.

    The queue is bounded by BOTH row count and serialized bytes.  The currently-exporting row remains
    charged to the byte budget until its delegate attempt finishes, so the resident prepared-row bound
    is real rather than excluding the worker's largest object.  Serialization stays synchronous; this
    is the price of exact accounting without an unbounded deepcopy of a mutable span record.

    A worker exits when ``force_flush`` drains it, on shutdown, or after a calm empty idle period.
    It is daemonized only so a forgotten explicit shutdown cannot wedge interpreter exit;
    normal Engine/finalization paths call ``force_flush`` and retire it deterministically.
    ``shutdown`` is one-shot and bounded, but—like OTel's processor timeout—it cannot interrupt a
    Python thread already inside filesystem I/O.  A timed-out worker never gets replaced or retried,
    preventing double export and unbounded thread accumulation.
    """

    def __init__(
        self,
        path: str | os.PathLike,
        *,
        run_id: str = "",
        lifecycle_fence: bool = False,
        max_queue_spans: int = TRACE_EXPORT_QUEUE_MAX_SPANS,
        max_queue_bytes: int = TRACE_EXPORT_QUEUE_MAX_BYTES,
        worker_idle_s: float = _TRACE_EXPORT_WORKER_IDLE_S,
        loss_receipt_interval_s: float = _TRACE_EXPORT_LOSS_RECEIPT_INTERVAL_S,
    ):
        if type(max_queue_spans) is not int or max_queue_spans <= 0:
            raise ValueError("max_queue_spans must be a positive int")
        if type(max_queue_bytes) is not int or max_queue_bytes <= 0:
            raise ValueError("max_queue_bytes must be a positive int")
        try:
            worker_idle_s = float(worker_idle_s)
            loss_receipt_interval_s = float(loss_receipt_interval_s)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("exporter timing bounds must be finite numbers") from exc
        if (not math.isfinite(worker_idle_s) or worker_idle_s <= 0
                or not math.isfinite(loss_receipt_interval_s)
                or loss_receipt_interval_s <= 0):
            raise ValueError("exporter timing bounds must be positive finite numbers")

        self.path = Path(path)
        self._lifecycle_fence = bool(lifecycle_fence)
        self._writer = JsonlSpanExporter(
            self.path, lifecycle_fence=self._lifecycle_fence)
        self._run_id = redact_persisted_identity(run_id, max_chars=_TRACE_RUN_ID_CAP)
        self._max_queue_spans = max_queue_spans
        self._max_queue_bytes = max_queue_bytes
        self._worker_idle_s = worker_idle_s
        self._loss_receipt_interval_s = loss_receipt_interval_s
        self._pid = os.getpid()
        self._instance_id = _hex(8)
        self._reset_process_state(accepting=True)

    def _reset_process_state(self, *, accepting: bool) -> None:
        """Create process-local synchronization and discard inherited parent work after fork."""
        self._condition = threading.Condition(threading.Lock())
        self._queue: deque[bytes] = deque()
        # Includes a row currently held by the worker until its delegate attempt returns.
        self._buffered_bytes = 0
        self._worker: Optional[threading.Thread] = None
        self._active = False
        self._retire_requested = False
        self._abandon_active = False
        self._accepting = accepting
        self._shutdown = not accepting
        self._accepted_spans = 0
        self._exported_spans = 0
        self._export_failures = 0
        self._drop_counts = {reason: 0 for reason in _TRACE_EXPORT_DROP_REASONS}
        self._unreported_drops = {reason: 0 for reason in _TRACE_EXPORT_DROP_REASONS}
        self._unreported_export_failures = 0
        self._loss_receipts = 0
        self._loss_receipt_failures = 0
        self._receipt_requested = False
        self._next_receipt_at = 0.0

    def _ensure_process(self) -> None:
        """A fork child starts empty; parent-owned queued rows must never be exported twice."""
        pid = os.getpid()
        if pid == self._pid:
            return
        # Do not touch the inherited Condition: it may have been locked by a vanished parent thread.
        accepting = self._accepting
        self._pid = pid
        self._instance_id = _hex(8)
        self._writer = JsonlSpanExporter(
            self.path, lifecycle_fence=self._lifecycle_fence)
        self._reset_process_state(accepting=accepting)

    @staticmethod
    def _timeout_deadline(timeout_millis: Optional[float]) -> Optional[float]:
        if timeout_millis is None:
            return None
        try:
            timeout = float(timeout_millis)
        except (TypeError, ValueError, OverflowError):
            return time.monotonic()
        if not math.isfinite(timeout) or timeout <= 0:
            return time.monotonic()
        return time.monotonic() + timeout / 1000.0

    def _start_worker_locked(self) -> bool:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return True
        try:
            worker = threading.Thread(
                target=self._worker_main,
                name=f"looplab-trace-export-{self._instance_id}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        except Exception:  # noqa: BLE001 - thread exhaustion must remain a non-throwing submit failure
            if self._worker is not None and self._worker.is_alive():
                return True  # defensive: never replace an ambiguously-started delegate
            self._worker = None
            return False
        return True

    def _record_drop_locked(
        self, reason: str, count: int = 1, *, durable: bool = True,
    ) -> None:
        count = max(0, int(count))
        if not count:
            return
        self._drop_counts[reason] += count
        if not durable:
            return
        self._unreported_drops[reason] += count
        # First loss is reported promptly. Later losses inside the receipt interval coalesce.
        if self._next_receipt_at <= 0:
            self._next_receipt_at = time.monotonic()

    def export(self, span: dict) -> bool:
        """Serialize and submit without waiting for the filesystem; False means explicitly dropped."""
        self._ensure_process()
        # Shutdown is a terminal writer boundary.  Check before caller-side serialization so an
        # invalid post-shutdown value cannot enter the exception path and resurrect a worker/loss
        # receipt behind reset.  The second check below closes the race with shutdown while a valid
        # accepted-candidate is being serialized.
        with self._condition:
            if not self._accepting:
                # A terminal exporter must never make a later idempotent shutdown resurrect a
                # receipt writer. Keep misuse observable in process, but not pending for disk.
                self._record_drop_locked("shutdown", durable=False)
                return False
        try:
            line = _span_jsonl_line(span)
        except Exception:  # noqa: BLE001 - the tracer's diagnostic boundary must stay non-throwing
            with self._condition:
                if not self._accepting:
                    self._record_drop_locked("shutdown", durable=False)
                else:
                    self._record_drop_locked("serialization_error")
                    self._start_worker_locked()
                    self._condition.notify_all()
            return False

        with self._condition:
            if not self._accepting:
                self._record_drop_locked("shutdown", durable=False)
                # Shutdown is one-shot: do not create a post-shutdown writer solely for misuse.
                self._condition.notify_all()
                return False
            self._retire_requested = False
            if len(self._queue) >= self._max_queue_spans:
                self._record_drop_locked("queue_full")
                self._start_worker_locked()
                self._condition.notify_all()
                return False
            if len(line) > self._max_queue_bytes - self._buffered_bytes:
                self._record_drop_locked("queue_bytes")
                self._start_worker_locked()
                self._condition.notify_all()
                return False
            self._queue.append(line)
            self._buffered_bytes += len(line)
            if not self._start_worker_locked():
                # No live worker owns the just-appended tail.  Undo admission atomically so ``True``
                # never means a row was stranded until an optional future call happens to retry.
                assert self._queue and self._queue[-1] is line
                self._queue.pop()
                self._buffered_bytes = max(0, self._buffered_bytes - len(line))
                self._record_drop_locked("worker_start")
                self._condition.notify_all()
                return False
            self._accepted_spans += 1
            self._condition.notify_all()
            return True

    def _loss_snapshot_locked(self) -> tuple[dict[str, int], int]:
        return (
            {reason: count for reason, count in self._unreported_drops.items() if count},
            self._unreported_export_failures,
        )

    def _loss_receipt(self, drops: dict[str, int], export_failures: int) -> dict:
        dropped = sum(drops.values())
        now = time.time()
        attributes = {
            "looplab.exporter.metric": "loss",
            "looplab.exporter.dropped_spans": dropped,
            "looplab.exporter.export_failures": export_failures,
            "looplab.exporter.queue_capacity_spans": self._max_queue_spans,
            "looplab.exporter.queue_capacity_bytes": self._max_queue_bytes,
            "looplab.exporter.instance_id": self._instance_id,
            "looplab.exporter.pid": self._pid,
        }
        for reason, count in drops.items():
            attributes[f"looplab.exporter.dropped.{reason}"] = count
        return {
            "name": "looplab.exporter.loss",
            "kind": "internal",
            "trace_id": _hex(16),
            "span_id": _hex(8),
            "parent_id": None,
            "run_id": self._run_id,
            "attributes": attributes,
            "events": [],
            "status": "ERROR",
            "start": now,
            "duration_s": 0.0,
        }

    def _worker_main(self) -> None:
        while True:
            item: Optional[bytes] = None
            loss: Optional[tuple[dict[str, int], int]] = None
            with self._condition:
                while True:
                    now = time.monotonic()
                    # Terminal timeout fencing wins over EVERY queued payload, including the loss
                    # receipt. Once engine.lock may be released, no old-process diagnostic may race
                    # a clear/reset rewrite. An active writer that already crossed pre_commit still
                    # owns TRACE_WRITER_LOCK_NAME; destructive work can only publish after it.
                    if self._abandon_active:
                        # Terminal ownership was released. No historical loss delta may cause a
                        # later shutdown/export call to resurrect this process as a writer.
                        self._unreported_drops = {
                            reason: 0 for reason in _TRACE_EXPORT_DROP_REASONS}
                        self._unreported_export_failures = 0
                        self._receipt_requested = False
                        self._worker = None
                        self._condition.notify_all()
                        return
                    pending_loss = (
                        any(self._unreported_drops.values())
                        or self._unreported_export_failures > 0
                    )
                    if pending_loss and (
                            self._receipt_requested or now >= self._next_receipt_at):
                        loss = self._loss_snapshot_locked()
                        self._receipt_requested = False
                        self._active = True
                        break
                    if self._queue:
                        item = self._queue.popleft()
                        # Keep its bytes charged until delegate export returns.
                        self._active = True
                        break
                    if self._shutdown or self._retire_requested:
                        self._worker = None
                        self._condition.notify_all()
                        return
                    if pending_loss:
                        self._condition.wait(max(
                            0.001, self._next_receipt_at - now))
                        continue
                    self._condition.wait(self._worker_idle_s)
                    if not self._queue and not any(self._unreported_drops.values()) \
                            and self._unreported_export_failures == 0:
                        self._worker = None
                        self._condition.notify_all()
                        return

            if loss is not None:
                drops, export_failures = loss
                succeeded = False
                try:
                    # Direct delegate call: a loss receipt cannot be evicted by the queue it reports.
                    receipt_line = _span_jsonl_line(
                        self._loss_receipt(drops, export_failures))
                    self._writer._export_line(
                        receipt_line, pre_commit=self._active_may_commit)
                    succeeded = True
                except _AsyncExportAbandoned:
                    pass
                except Exception:  # noqa: BLE001 - retain the delta for a later export/flush attempt
                    pass
                with self._condition:
                    self._active = False
                    # Consume this exact delta after ONE delegate attempt whether it returned or
                    # raised. The write may have committed before a descriptor/path post-validation
                    # failed; retrying would make the durable summed metric count the same loss twice.
                    for reason, count in drops.items():
                        self._unreported_drops[reason] = max(
                            0, self._unreported_drops[reason] - count)
                    self._unreported_export_failures = max(
                        0, self._unreported_export_failures - export_failures)
                    if succeeded:
                        self._loss_receipts += 1
                        self._next_receipt_at = (
                            time.monotonic() + self._loss_receipt_interval_s)
                    else:
                        self._loss_receipt_failures += 1
                        self._next_receipt_at = (
                            time.monotonic() + self._loss_receipt_interval_s)
                        # Do not leave a daemon retrying forever on a dead filesystem. New deltas can
                        # trigger a new receipt later; this ambiguous delta is never attempted twice.
                        if not self._queue:
                            self._worker = None
                            self._condition.notify_all()
                            return
                    self._condition.notify_all()
                continue

            assert item is not None
            succeeded = False
            abandoned = False
            try:
                # Exactly one delegate attempt. Retrying after an exception could duplicate a row
                # whose bytes committed before a post-write identity/visibility check failed.
                self._writer._export_line(item, pre_commit=self._active_may_commit)
                succeeded = True
            except _AsyncExportAbandoned:
                abandoned = True
                with self._condition:
                    self._record_drop_locked(
                        "shutdown_timeout", durable=False)
            except Exception:  # noqa: BLE001 - diagnostics never crash the observed operation
                pass
            with self._condition:
                self._buffered_bytes = max(0, self._buffered_bytes - len(item))
                self._active = False
                if succeeded:
                    self._exported_spans += 1
                elif not abandoned:
                    self._export_failures += 1
                    if not self._abandon_active:
                        self._unreported_export_failures += 1
                        if self._next_receipt_at <= 0:
                            self._next_receipt_at = time.monotonic()
                self._condition.notify_all()

    def _terminal_abandon_locked(self) -> None:
        """Retire not-started work and all durable receipt intent before ownership release."""
        pending = len(self._queue)
        if pending:
            pending_bytes = sum(map(len, self._queue))
            self._queue.clear()
            self._buffered_bytes = max(0, self._buffered_bytes - pending_bytes)
            self._record_drop_locked(
                "shutdown_timeout", pending, durable=False)
        self._unreported_drops = {reason: 0 for reason in _TRACE_EXPORT_DROP_REASONS}
        self._unreported_export_failures = 0
        self._receipt_requested = False
        self._abandon_active = True
        self._condition.notify_all()

    def _active_may_commit(self) -> bool:
        """Rechecked only after the shared trace-writer guard is held (see `_export_line`)."""
        with self._condition:
            return not self._abandon_active

    def metrics(self) -> dict[str, int | bool]:
        """Return a process-local, race-consistent exporter health snapshot."""
        self._ensure_process()
        with self._condition:
            return {
                "accepted_spans": self._accepted_spans,
                "exported_spans": self._exported_spans,
                "dropped_spans": sum(self._drop_counts.values()),
                "export_failures": self._export_failures,
                **{f"dropped_{reason}": self._drop_counts[reason]
                   for reason in _TRACE_EXPORT_DROP_REASONS},
                "queued_spans": len(self._queue),
                "buffered_bytes": self._buffered_bytes,
                "loss_receipts": self._loss_receipts,
                "loss_receipt_failures": self._loss_receipt_failures,
                "worker_alive": bool(self._worker is not None and self._worker.is_alive()),
                "shutdown": self._shutdown,
            }

    def force_flush(
        self,
        timeout_millis: Optional[float] = TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS,
    ) -> bool:
        """Wait for work accepted before/during the barrier and its loss receipt.

        What is COVERED: every row accepted before or during the barrier has had its one delegate
        attempt RETURN before this call does — the wait clears on `not self._queue and not
        self._active and not worker_alive`, so a row already handed to the writer holds the barrier
        exactly like a waiting one.  A queue-only barrier would not, which is what
        `tests/test_tracing.py::test_force_flush_waits_for_a_row_already_handed_to_the_writer`
        drives.

        What is NOT covered, and the return value says so about neither: an accepted row gets
        EXACTLY ONE attempt (retrying an ambiguous append could double-export), so a row whose
        attempt RAISED is permanently absent from `spans.jsonl` while this still returns True.
        `True` means the loss RECEIPT did not fail; it never means every accepted span is on disk.
        Loss is readable only in `metrics()["export_failures"]` and in the durable coalesced
        `looplab.exporter.loss` row this same barrier emits.  A caller that needs "the artifact I am
        about to read contains every span I queued" must read those, not this boolean.

        The timeout bounds only the caller's wait; Python cannot safely interrupt a worker already in
        filesystem I/O.  False therefore never claims that the underlying call was cancelled.
        """
        self._ensure_process()
        deadline = self._timeout_deadline(timeout_millis)
        with self._condition:
            baseline_receipt_failures = self._loss_receipt_failures
            if any(self._unreported_drops.values()) or self._unreported_export_failures:
                self._receipt_requested = True
            self._retire_requested = True
            if (self._queue or self._active or self._receipt_requested) \
                    and not self._start_worker_locked():
                return False
            # Also wake a calm idle worker so this barrier retires it immediately instead of waiting
            # for the long anti-churn idle timeout.
            self._condition.notify_all()
            while True:
                pending_loss = (
                    any(self._unreported_drops.values())
                    or self._unreported_export_failures > 0
                )
                worker_alive = bool(self._worker is not None and self._worker.is_alive())
                if not self._queue and not self._active and not pending_loss and not worker_alive:
                    return self._loss_receipt_failures == baseline_receipt_failures
                if (pending_loss and not worker_alive
                        and self._loss_receipt_failures > baseline_receipt_failures):
                    return False
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                else:
                    remaining = None
                self._condition.wait(remaining)

    def shutdown(
        self,
        timeout_millis: Optional[float] = TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS,
    ) -> bool:
        """Stop accepting, drain once, and join the one worker within the caller's wait budget."""
        self._ensure_process()
        deadline = self._timeout_deadline(timeout_millis)
        with self._condition:
            baseline_receipt_failures = self._loss_receipt_failures
            self._accepting = False
            self._shutdown = True
            self._retire_requested = True
            if any(self._unreported_drops.values()) or self._unreported_export_failures:
                self._receipt_requested = True
            if (self._queue or self._active or self._receipt_requested) \
                    and not self._start_worker_locked():
                self._terminal_abandon_locked()
                return False
            self._condition.notify_all()
            while True:
                pending_loss = (
                    any(self._unreported_drops.values())
                    or self._unreported_export_failures > 0
                )
                worker_alive = bool(self._worker is not None and self._worker.is_alive())
                if not self._queue and not self._active and not pending_loss and not worker_alive:
                    return self._loss_receipt_failures == baseline_receipt_failures
                if (pending_loss and not worker_alive
                        and self._loss_receipt_failures > baseline_receipt_failures):
                    return False
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Pending rows have not started and can be definitely retired. The active row
                        # is NOT counted/dropped here: its uninterruptible delegate attempt may still
                        # succeed, and the worker remains the sole owner until it returns.
                        self._terminal_abandon_locked()
                        return False
                else:
                    remaining = None
                self._condition.wait(remaining)

    close = shutdown

    def __enter__(self) -> "AsyncJsonlSpanExporter":
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()


class SpanHandle:
    """Handle yielded by `Tracer.span` to enrich the span after it opens: attributes (e.g. the
    metric/exit once known) and point-in-time events (e.g. a tool call). Mirrors to OTel."""

    def __init__(self, rec: dict, otel_span):
        self._rec = rec
        self._otel = otel_span

    # `spans.jsonl` and the OTLP exporter are a DURABLE egress boundary, so redaction has to happen
    # here rather than at the call site. Every purpose-built path already sanitizes before writing
    # (`generation`, `tool`, `output`, `thinking`), but these generic methods took arbitrary values
    # verbatim — and `traceview` only protects the UI projection, not the bytes on disk or the ones
    # shipped to an external collector. Both helpers are idempotent over already-sanitized strings, so
    # routing the specialized paths' output through them again costs nothing.
    @staticmethod
    def _safe(key: str, value):
        return "***" if is_secret_key_name(key) else sanitize_trace_value(value)

    @staticmethod
    def _safe_key(key, *, event_field: bool = False) -> tuple[str, str]:
        """Return (raw, durable) key spellings; raw still controls secret-value masking."""
        raw = str(key)
        durable = _trace_label(raw, cap=_TRACE_META_CAP).strip() or "field"
        # The event envelope owns `name`; never let a sanitized dynamic field overwrite it.
        if event_field and durable == "name":
            durable = "event.name"
        # Sanitization can collapse a different raw spelling (for example ``generation\n``) onto a
        # lifecycle key. Never let arbitrary metadata overwrite the topology/index fence merely by
        # normalizing to its name. The exact supported spelling remains available to engine code.
        if not event_field and durable in _TRACE_RESERVED_ATTRIBUTE_KEYS and raw != durable:
            durable = _trace_label(f"field.{durable}", cap=_TRACE_META_CAP)
        return raw, durable

    def set(self, key: str, value) -> "SpanHandle":
        raw_key, key = self._safe_key(key)
        value = self._safe(raw_key, value)
        # Exact structural setters are privileged only with their schema shape. Invalid provider
        # metadata is retained as diagnostic data without corrupting node/attempt attribution.
        if raw_key in ("generation", "attempt") and not (
                type(value) is int and value >= 0):
            key = f"field.{key}"
        elif raw_key == "node_id" and not (
                (type(value) is int and -(1 << 63) <= value <= (1 << 63) - 1)
                or (isinstance(value, str) and 0 < len(value) <= 128)):
            key = f"field.{key}"
        elif raw_key == "build_trace" and not (
                isinstance(value, str) and 0 < len(value) <= 128):
            # A claim on another trace is only readable if it is a trace ID SHAPE. Anything else is
            # kept as diagnostic payload rather than silently offered to the reader's join.
            key = f"field.{key}"
        elif raw_key == "looplab.run_id":
            key = "field.looplab.run_id"
        self._rec["attributes"][key] = value
        # Some production roots (notably evaluate) learn their attempt only after preflight and call
        # `sp.set("generation", n)`. Descendants open before the root is exported, so update the
        # active root context as well as its record; Tracer.span's original context token still
        # restores the enclosing lifecycle on exit. A stale/non-current handle must never retag the
        # task's active trace.
        stack = _stack.get()
        if (raw_key == "generation" and type(value) is int and value >= 0
                and stack and stack[-1] is self._rec
                and self._rec.get("parent_id") is None):
            _generation_ctx.set(value)
        if self._otel is not None:
            try:
                self._otel.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))
            except Exception:  # noqa: BLE001
                pass
        return self

    def set_many(self, **kv) -> "SpanHandle":
        for k, v in kv.items():
            self.set(k, v)
        return self

    def event(self, name: str, **fields) -> "SpanHandle":
        name = _trace_label(name, cap=_TRACE_META_CAP)
        safe_fields = {}
        for raw_key, value in fields.items():
            secret_key, durable_key = self._safe_key(raw_key, event_field=True)
            safe_fields[durable_key] = self._safe(secret_key, value)
        fields = safe_fields
        self._rec["events"].append({"name": name, **fields})
        if self._otel is not None:
            try:
                self._otel.add_event(name, {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                                            for k, v in fields.items()})
            except Exception:  # noqa: BLE001
                pass
        return self


class Tracer:
    def __init__(self, exporter: JsonlSpanExporter | AsyncJsonlSpanExporter, run_id: str = "",
                 capture_llm_io: Optional[bool] = None):
        # Optional providers are often configured by the embedding application after importing
        # LoopLab.  The import-time probe is only a fast path; retry once per Tracer construction
        # while the bridge is absent so a real provider installed before this run is discovered.
        # Keep the resolved tracer on the instance: a later process-global change cannot alter an
        # already-running LoopLab tracer halfway through its span tree.
        global _OTEL
        sdk_disabled = _otel_sdk_disabled()
        if not sdk_disabled and _OTEL is None:
            try:
                _OTEL = _init_otel()
            except Exception:  # noqa: BLE001 - diagnostics must never block engine construction
                _OTEL = None
        self._otel = None if sdk_disabled else _OTEL
        self.exporter = exporter
        # run_id is an opaque join key, so use the identity sanitizer: unlike display prose it does
        # not NFKC-fold distinct benign identifiers into one another.  It still masks credential
        # shapes, strips controls and bounds untrusted embedding callers before JSONL/OTLP egress.
        self.run_id = redact_persisted_identity(run_id, max_chars=_TRACE_RUN_ID_CAP)
        # THIS tracer's LLM-I/O capture policy (see `_CAPTURE_LLM_IO`): the run's own decision, bound
        # to every span it opens so a concurrent run with the opposite policy cannot leak into it.
        # None => defer to the process-wide default, keeping a bare `Tracer(...)` byte-identical.
        self.capture_llm_io = None if capture_llm_io is None else bool(capture_llm_io)

    def force_flush(
        self,
        timeout_millis: Optional[float] = TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS,
    ) -> bool:
        """Settle every accepted local span's export attempt before a reader/owner boundary.

        Deliberately NOT "make all accepted spans visible": the async exporter gives each accepted
        row one delegate attempt and reports a failed attempt through its loss receipt, not through
        this boolean (see `AsyncJsonlSpanExporter.force_flush`).  So a reader behind this barrier
        sees every span whose write SUCCEEDED plus a `looplab.exporter.loss` row counting the ones
        that did not — never a silent hole.

        The synchronous exporter has no pending work. Optional/custom exporters without the method
        retain that compatibility behavior; an exporter implementation failure stays diagnostic and
        returns False rather than perturbing domain control flow.
        """
        flush = getattr(self.exporter, "force_flush", None)
        if not callable(flush):
            return True
        try:
            return bool(flush(timeout_millis=timeout_millis))
        except Exception:  # noqa: BLE001 - tracing must never become a domain failure
            return False

    def shutdown(
        self,
        timeout_millis: Optional[float] = TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS,
    ) -> bool:
        """One-shot exporter shutdown for embedding applications that own the Tracer lifetime."""
        shutdown = getattr(self.exporter, "shutdown", None)
        if not callable(shutdown):
            return True
        try:
            return bool(shutdown(timeout_millis=timeout_millis))
        except Exception:  # noqa: BLE001 - tracing must never become a domain failure
            return False

    @contextmanager
    def span(self, name: str, *, new_trace: bool = False, kind: str = "operation", **attributes):
        # Span metadata reaches TWO durable egresses: spans.jsonl and, when configured, OTLP.  Sanitize
        # before using it for context propagation or opening the OTel span so constructor kwargs cannot
        # bypass the boundary enforced by SpanHandle.set/event.  The tree walker applies one aggregate
        # character/item/depth budget to the initial attribute map and masks secret-named nested fields;
        # primitive benign values retain their ordinary JSON semantics.
        name = _trace_label(name, cap=_TRACE_SPAN_NAME_CAP)
        kind = _trace_label(kind, cap=_TRACE_SPAN_KIND_CAP)
        attributes = _sanitize_initial_attributes(attributes)
        st = _stack.get()
        parent = st[-1] if st else None
        # Propagate node_id via a contextvar (see _node_ctx): a span that names an explicit node_id sets
        # it for the block; a span that doesn't INHERITS the active one, so nested generation/tool spans
        # get attributed to the node even when they open in a trace of their own. Stamp onto attributes
        # so the on-disk span (and the trace view that reads it) carries it.
        _nid_own = attributes.get("node_id")
        if _nid_own is None and _node_ctx.get() is not None:
            attributes = {**attributes, "node_id": _node_ctx.get()}
        _tok_node = _node_ctx.set(attributes["node_id"]) if attributes.get("node_id") is not None else None
        # Lifecycle generation is a trace-root property.  Descendants in the same trace inherit the
        # active value even if an observation accidentally supplies a different retry label; a nested
        # new_trace may explicitly establish another lifecycle, and token reset restores the outer one.
        _active_generation = _generation_ctx.get()
        _own_generation = attributes.get("generation")
        _own_generation = (_own_generation if type(_own_generation) is int
                           and _own_generation >= 0 else None)
        if _active_generation is not None and (not new_trace or _own_generation is None):
            _lifecycle_generation = _active_generation
        else:
            _lifecycle_generation = _own_generation
        if _lifecycle_generation is not None:
            attributes = {**attributes, "generation": _lifecycle_generation}
        _tok_generation = _generation_ctx.set(_lifecycle_generation)
        # Phase attribution (see _phase_ctx): a non-operation child (generation/tool) inherits the active
        # phase so the trace view bands it correctly LIVE, before its parent operation span is flushed. An
        # operation span sets the phase to ITS name for the block (so its own children inherit it).
        # `phase_span` carries the op's SPAN ID alongside the name: the name alone can't distinguish two
        # same-phase sub-loops in one node (a ValidatingDeveloper retry re-runs stages→plan→implement),
        # so a live view keyed on the bare name merged attempt 2's turns into attempt 1's band.
        _ph = _phase_ctx.get()          # (name, span_id) of the innermost open operation, or None
        if kind != "operation" and _ph is not None and attributes.get("phase") is None:
            attributes = {**attributes, "phase": _ph[0], "phase_span": _ph[1]}
        _tok_phase = None
        # Reset the delta-encode chain (`_prev_gen`) at a TRACE boundary: chaining never crosses traces
        # (generation()'s tid-guard enforces it), so a new trace always starts from a fresh base — and
        # this bounds the retained full-input list to the trace's lifetime (reset in `finally`), matching
        # the token discipline of the sibling contextvars above instead of leaking the last generation's
        # (MB-scale) message list past the run into an idle context.
        _tok_prev = _prev_gen.set(None) if new_trace else None
        # Bind THIS tracer's capture policy for the block (see `_capture_ctx`). ALWAYS set, even when
        # the tracer declared nothing: an undeclared tracer must fall back to the PROCESS default, not
        # inherit whatever policy a concurrently-open span of another run left in this context.
        _tok_cap = _capture_ctx.set(self.capture_llm_io)
        # The context tokens above are reset only in the `finally` below — guard the one OTel call
        # between them so a broken bridged provider can't raise past them and leak a stale
        # node_id/phase onto every later span in this task (spans are diagnostics: degrade, not raise).
        otel_cm = otel_span = None
        if self._otel is not None:
            try:
                # `new_trace` must isolate BOTH id chains. Without an explicit empty context the OTel
                # span inherits the ambient one and stays a child in the collector's trace, so the
                # JSONL and OTLP views of the same operation disagree about where the trace starts.
                _otel_ctx = None
                if new_trace:
                    try:
                        from opentelemetry.context import Context as _OtelContext
                        _otel_ctx = _OtelContext()
                    except Exception:  # noqa: BLE001 - older/absent API: fall back to ambient context
                        _otel_ctx = None
                otel_cm = (self._otel.start_as_current_span(name, context=_otel_ctx)
                           if _otel_ctx is not None else self._otel.start_as_current_span(name))
                otel_span = otel_cm.__enter__()
            except Exception:  # noqa: BLE001
                otel_cm = otel_span = None
        # IDs come from the real OTel span when bridged (so JSONL and the collector agree),
        # else we generate OTel-shaped ids ourselves (16-byte trace / 8-byte span, hex).
        trace_id = span_id = None
        if otel_span is not None:
            try:
                ctx = otel_span.get_span_context()
                # Only trust the OTel ids when a real (recording) provider is configured; with
                # the API present but no SDK provider, spans are non-recording with an INVALID
                # all-zero context — fall back to our own ids so nesting/grouping still work.
                if getattr(ctx, "is_valid", False):
                    trace_id, span_id = format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
            except Exception:  # noqa: BLE001
                pass
        if trace_id is None:
            trace_id = parent["trace_id"] if (parent and not new_trace) else _hex(16)
            span_id = _hex(8)
        if kind == "operation":        # ids are final only here — children stamp (name, THIS span's id)
            _tok_phase = _phase_ctx.set((name, span_id))
        # `new_trace` gives this span a fresh trace_id above; keeping the parent's span_id here left it
        # pointing ACROSS traces, so `traceview` (which finds a trace's root by `parent_id is None`)
        # found no root for the new trace and rendered it orphaned under the outer one.
        parent_id = parent["span_id"] if (parent and not new_trace) else None
        # kind ∈ {operation, generation, tool, retrieval}: the Langfuse-style observation type, so the
        # UI can render generations (LLM calls) and tools distinctly from plain operation spans.
        rec = {"name": name, "kind": kind, "trace_id": trace_id, "span_id": span_id,
               "parent_id": parent_id, "run_id": self.run_id, "attributes": dict(attributes),
               "events": [], "status": "OK"}
        if otel_span is not None:
            for k, v in attributes.items():
                try:
                    otel_span.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
                except Exception:  # noqa: BLE001
                    pass
            try:
                # The random trace id is insufficient to join concurrent LoopLab runs in an external
                # collector.  Keep the opaque, already-sanitized run identity on every mirrored span;
                # write it last so an embedding caller cannot override this correlation attribute.
                otel_span.set_attribute("looplab.run_id", self.run_id)
            except Exception:  # noqa: BLE001
                pass
        token = _stack.set(st + (rec,))
        # Expose THIS tracer to nested code (LLM client / tool loop) so generation()/tool() open
        # child observations against the right run's exporter. Reset with the stack in `finally`.
        tok_tr = _current_tracer.set(self)
        start, mono0 = time.time(), time.monotonic()
        try:
            yield SpanHandle(rec, otel_span)
        except BaseException as e:  # noqa: BLE001 - record on the span, then re-raise
            rec["status"] = "ERROR"
            safe_error = _redacted_error(e)
            safe_error_type = _trace_label(type(e).__name__, cap=_TRACE_META_CAP)
            rec["events"].append({"name": "exception", "error": safe_error,
                                  "type": safe_error_type})
            # Never hand the raw exception to the OTel context manager: its automatic
            # record_exception path would export the original provider message verbatim. Mirror only a
            # sanitized event/status and close the context normally in `finally`.
            if otel_span is not None:
                try:
                    from opentelemetry.trace import Status, StatusCode
                    otel_span.add_event("exception", {
                        "exception.type": safe_error_type,
                        "exception.message": safe_error,
                    })
                    otel_span.set_status(Status(StatusCode.ERROR, description=safe_error))
                except Exception:  # noqa: BLE001 - mirroring must never mask the original failure
                    pass
            raise
        finally:
            rec["start"] = start
            # monotonic delta: a wall-clock (time.time) delta can go negative on an NTP/clock step
            rec["duration_s"] = round(time.monotonic() - mono0, 6)
            if otel_cm is not None:
                try:
                    # ERROR status/event were set above from sanitized text. A normal exit prevents the
                    # OTel SDK from auto-recording the raw exception and leaking its message.
                    otel_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            _stack.reset(token)
            _current_tracer.reset(tok_tr)
            if _tok_node is not None:
                _node_ctx.reset(_tok_node)
            _generation_ctx.reset(_tok_generation)
            if _tok_phase is not None:
                _phase_ctx.reset(_tok_phase)
            if _tok_prev is not None:
                _prev_gen.reset(_tok_prev)     # restore the outer trace's chain (or None at top level)
            _capture_ctx.reset(_tok_cap)       # restore the enclosing run's policy (or None = process)
            try:
                self.exporter.export(rec)
            except Exception:  # noqa: BLE001 - spans are diagnostics: an export failure (disk full,
                pass           # a non-serializable attribute) must never mask the in-flight exception
                #                or crash the traced engine operation.
