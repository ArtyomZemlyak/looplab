"""Observability (I14, ADR-17): full tracing that stays files-as-truth by default and bridges
to real OpenTelemetry when the SDK is installed.

Design (see 08-tracing-architecture.md):
- We instrument with one tracer facade (`Tracer.span`). Spans nest via a contextvar stack, so
  the JSONL we always emit (`spans.jsonl`) is a real trace tree (trace_id / span_id /
  parent_id), not a flat log. This is the zero-dependency, offline default.
- When `opentelemetry-api` (+ an SDK/exporter configured via OTEL_* env) is importable, each
  span is ALSO opened as a genuine OpenTelemetry span, so ANY OTLP collector (Jaeger / Tempo /
  Honeycomb / …) receives it with no code change. Without the package the bridge is a no-op.
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
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import orjson

# Active span stack (per async task / thread — contextvars are copied across anyio.to_thread
# and task spawns, so nesting works through the worker-thread eval too).
_stack: contextvars.ContextVar[tuple] = contextvars.ContextVar("LOOPLAB_spans", default=())

def _init_otel():  # pragma: no cover - exercised only with the [otel] extra installed
    """Return an OpenTelemetry tracer if the SDK is installed, configuring an OTLP exporter
    from the standard OTEL_* env when an endpoint is set (so `OTEL_EXPORTER_OTLP_ENDPOINT=…`
    sends spans to any collector with no code change). Any problem -> None (bridge off, the
    JSONL exporter still works). Operators can also use the `opentelemetry-instrument` CLI,
    which configures the global provider itself; we just call get_tracer in that case."""
    try:
        from opentelemetry import trace as _otrace
    except Exception:  # noqa: BLE001 - package absent -> bridge off
        return None
    try:
        # If an endpoint/exporter is configured but no real provider is installed yet, wire a
        # batched OTLP exporter once. (No-op if a provider is already set, e.g. by the CLI.)
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_TRACES_EXPORTER"):
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


# LLM I/O capture (ADR-17): off by default at import; the engine flips it on per run from
# Settings.trace_llm_io. When on, record_llm_call attaches the prompt+completion as a span
# event on whatever operation span is active (propose/implement/repair), so the UI sees exactly
# what the model read and wrote — without a new transport or any change to the event log.
_CAPTURE_LLM_IO = False


def set_llm_capture(enabled: bool) -> None:
    global _CAPTURE_LLM_IO
    _CAPTURE_LLM_IO = bool(enabled)


def record_llm_call(*, op: str, model: str, messages: list[dict], completion: str,
                    usage: Optional[dict] = None) -> None:
    """Append an `llm_call` event to the active span record (no-op when capture is off or there
    is no active span — e.g. an LLM call outside any traced operation). Full text by design;
    secrets are masked upstream at the config layer, prompts/completions are task content."""
    if not _CAPTURE_LLM_IO:
        return
    st = _stack.get()
    if not st:
        return
    rec = st[-1]
    tokens = {
        "prompt": int((usage or {}).get("prompt_tokens") or 0),
        "completion": int((usage or {}).get("completion_tokens") or 0),
        "total": int((usage or {}).get("total_tokens") or 0),
    }
    rec["events"].append({
        "name": "llm_call",
        "op": op,
        "model": model,
        # store messages verbatim (role + content) so the UI can render the conversation
        "prompt": [{"role": m.get("role", "user"), "content": _as_text(m.get("content"))}
                   for m in (messages or [])],
        "completion": completion or "",
        "tokens": tokens,
    })
    # Mirror to the active OpenTelemetry span when the bridge is live, so OTLP collectors
    # (Jaeger/Tempo/…) see LLM I/O too — not just spans.jsonl. Best-effort; primitives only.
    if _OTEL is not None:
        try:
            from opentelemetry import trace as _ot
            _ot.get_current_span().add_event("llm_call", {
                "op": op, "model": model,
                "prompt_tokens": tokens["prompt"], "completion_tokens": tokens["completion"],
                "total_tokens": tokens["total"],
                "completion": (completion or "")[:2000],
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


class JsonlSpanExporter:
    """Default exporter: one JSON span per line in `spans.jsonl` (files-as-truth, offline)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import threading
        self._lock = threading.Lock()   # spans export from worker threads (to_thread eval)

    def export(self, span: dict) -> None:
        line = orjson.dumps(span) + b"\n"
        # Serialize writes: child spans export from to_thread worker threads while the parent
        # span exports from the event-loop thread; without the lock concurrent appends from
        # distinct handles can interleave into a corrupt JSON line (max_parallel>1).
        with self._lock, open(self.path, "ab") as f:
            f.write(line)


class SpanHandle:
    """Handle yielded by `Tracer.span` to enrich the span after it opens: attributes (e.g. the
    metric/exit once known) and point-in-time events (e.g. a tool call). Mirrors to OTel."""

    def __init__(self, rec: dict, otel_span):
        self._rec = rec
        self._otel = otel_span

    def set(self, key: str, value) -> "SpanHandle":
        self._rec["attributes"][key] = value
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
        self._rec["events"].append({"name": name, **fields})
        if self._otel is not None:
            try:
                self._otel.add_event(name, {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                                            for k, v in fields.items()})
            except Exception:  # noqa: BLE001
                pass
        return self


class Tracer:
    def __init__(self, exporter: JsonlSpanExporter, run_id: str = ""):
        self.exporter = exporter
        self.run_id = run_id

    @contextmanager
    def span(self, name: str, *, new_trace: bool = False, **attributes):
        st = _stack.get()
        parent = st[-1] if st else None
        otel_cm = _OTEL.start_as_current_span(name) if _OTEL is not None else None
        otel_span = otel_cm.__enter__() if otel_cm is not None else None
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
        parent_id = parent["span_id"] if parent else None
        rec = {"name": name, "trace_id": trace_id, "span_id": span_id, "parent_id": parent_id,
               "run_id": self.run_id, "attributes": dict(attributes), "events": [], "status": "OK"}
        if otel_span is not None:
            for k, v in attributes.items():
                try:
                    otel_span.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
                except Exception:  # noqa: BLE001
                    pass
        token = _stack.set(st + (rec,))
        start, mono0 = time.time(), time.monotonic()
        exc: BaseException | None = None
        try:
            yield SpanHandle(rec, otel_span)
        except BaseException as e:  # noqa: BLE001 - record on the span, then re-raise
            exc = e
            rec["status"] = "ERROR"
            rec["events"].append({"name": "exception", "error": repr(e)[:500]})
            # NB: don't manually record_exception here — the OTel cm's __exit__ (below) records
            # it AND sets ERROR status when we pass the exc info, so doing both double-logs it.
            raise
        finally:
            rec["start"] = start
            # monotonic delta: a wall-clock (time.time) delta can go negative on an NTP/clock step
            rec["duration_s"] = round(time.monotonic() - mono0, 6)
            if otel_cm is not None:
                # Pass the real exc info so the OTel SDK sets the span status to ERROR (its
                # __exit__ defaults to set_status_on_exception=True) — keeps Jaeger/Tempo in
                # sync with spans.jsonl instead of showing the failed span as OK.
                try:
                    if exc is not None:
                        otel_cm.__exit__(type(exc), exc, exc.__traceback__)
                    else:
                        otel_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            _stack.reset(token)
            self.exporter.export(rec)
