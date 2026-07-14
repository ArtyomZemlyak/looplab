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
# most wants to see (what the agent thought + which tools it called). Copied across task/thread spawns
# like the other contextvars, so it survives the worker-thread eval + anyio.to_thread offloads.
_node_ctx: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_node", default=None)

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
                    thinking: Optional[str] = None, usage: Optional[dict] = None) -> None:
    """Append an `llm_call` event to the active span record (no-op when capture is off or there
    is no active span — e.g. an LLM call outside any traced operation). Full text by design;
    secrets are masked upstream at the config layer, prompts/completions are task content.

    `completion` is the model's clean answer (the conclusion the UI surfaces). `thinking`, when
    present, is the raw <think> chain-of-thought — stored on a SEPARATE field so the UI can keep
    it as a collapsed debug-only disclosure rather than the primary view."""
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
    ev = {
        "name": "llm_call",
        "op": op,
        "model": model,
        # store messages verbatim (role + content) so the UI can render the conversation
        "prompt": [{"role": m.get("role", "user"), "content": _as_text(m.get("content"))}
                   for m in (messages or [])],
        "completion": completion or "",
        "tokens": tokens,
    }
    if thinking:
        ev["thinking"] = thinking
    rec["events"].append(ev)
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


def _norm_usage(tokens) -> dict:
    """Accept either OpenAI usage ({prompt_tokens,…}) or our short form ({prompt,…})."""
    t = tokens or {}
    p = int(t.get("prompt_tokens") or t.get("prompt") or 0)
    c = int(t.get("completion_tokens") or t.get("completion") or 0)
    return {"prompt": p, "completion": c, "total": int(t.get("total_tokens") or t.get("total") or (p + c))}


class ObservationHandle:
    """Fluent handle for a first-class observation (a `generation` = one LLM call, or a `tool` = one
    tool invocation) — the Langfuse-style tree node. Wraps a SpanHandle (or None when untraced) and
    lets the caller attach the OUTPUT / token usage / cost / reasoning AFTER the call returns, so the
    span carries the full input→output record with real latency (the enclosing span's duration)."""

    def __init__(self, h: "SpanHandle | None"):
        self._h = h

    @property
    def active(self) -> bool:
        return self._h is not None

    def set(self, key: str, value) -> "ObservationHandle":
        if self._h is not None:
            self._h.set(key, value)
        return self

    def output(self, text) -> "ObservationHandle":
        if self._h is not None and _CAPTURE_LLM_IO and text is not None:
            self._h.set("output", text if isinstance(text, str) else _as_text(text))
        return self

    def usage(self, tokens) -> "ObservationHandle":
        if self._h is not None and tokens:
            self._h.set("usage", _norm_usage(tokens))
        return self

    def cost(self, c) -> "ObservationHandle":
        if self._h is not None and c is not None:
            try:
                self._h.set("cost", float(c))
            except (TypeError, ValueError):
                pass
        return self

    def thinking(self, t) -> "ObservationHandle":
        if self._h is not None and _CAPTURE_LLM_IO and t:
            self._h.set("thinking", t)
        return self

    def error(self, msg: str) -> "ObservationHandle":
        if self._h is not None:
            self._h.set("level", "ERROR").event("exception", error=str(msg)[:500])
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
        attrs["model_parameters"] = model_parameters
    with tr.span("generation", kind="generation", **attrs) as h:
        if messages is not None and _CAPTURE_LLM_IO:
            cur = [{"role": m.get("role", "user"), "content": _as_text(m.get("content"))}
                   for m in messages]
            # Delta-encode the re-sent history: when this generation STRICTLY EXTENDS the prior one IN
            # THIS TRACE (only appended to it), store just the appended tail, plus a back-ref
            # (`input_from`) + carried-prefix count (`input_carry`). A fresh trace / sub-loop whose
            # history diverges just stores a full base (input_from=None). ~6x smaller spans.jsonl;
            # `/spans/{sid}` and `/trace/by_trace`
            # reconstruct the full verbatim input from the chain (traceview.hydrate_inputs). The reader
            # tolerates old logs (no input_carry ⇒ the `input` IS the full list).
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
    with tr.span("tool", kind="tool", tool=name) as h:
        if arguments is not None and _CAPTURE_LLM_IO:
            h.set("input", arguments if isinstance(arguments, (str, int, float, bool, dict, list))
                  else str(arguments))
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


class JsonlSpanExporter:
    """Default exporter: one JSON span per line in `spans.jsonl` (files-as-truth, offline)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import threading
        self._lock = threading.Lock()   # spans export from worker threads (to_thread eval)

    def export(self, span: dict) -> None:
        # default=str so a stray non-serializable attribute stashed on the span doesn't drop it.
        line = orjson.dumps(span, default=str) + b"\n"
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
    def span(self, name: str, *, new_trace: bool = False, kind: str = "operation", **attributes):
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
        # The context tokens above are reset only in the `finally` below — guard the one OTel call
        # between them so a broken bridged provider can't raise past them and leak a stale
        # node_id/phase onto every later span in this task (spans are diagnostics: degrade, not raise).
        otel_cm = otel_span = None
        if _OTEL is not None:
            try:
                otel_cm = _OTEL.start_as_current_span(name)
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
        parent_id = parent["span_id"] if parent else None
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
        token = _stack.set(st + (rec,))
        # Expose THIS tracer to nested code (LLM client / tool loop) so generation()/tool() open
        # child observations against the right run's exporter. Reset with the stack in `finally`.
        tok_tr = _current_tracer.set(self)
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
            _current_tracer.reset(tok_tr)
            if _tok_node is not None:
                _node_ctx.reset(_tok_node)
            if _tok_phase is not None:
                _phase_ctx.reset(_tok_phase)
            if _tok_prev is not None:
                _prev_gen.reset(_tok_prev)     # restore the outer trace's chain (or None at top level)
            try:
                self.exporter.export(rec)
            except Exception:  # noqa: BLE001 - spans are diagnostics: an export failure (disk full,
                pass           # a non-serializable attribute) must never mask the in-flight exception
                #                or crash the traced engine operation.
