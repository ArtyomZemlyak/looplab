# ADR-08 — Tracing / observability architecture

## Status
Accepted (2026-06-22). Supersedes the I14 "stub span exporter" note.

## Context
We need **full** tracing (LLM calls, agent subprocess, eval phases, decisions, errors, timing)
that simultaneously:
1. keeps the **files-as-truth** base;
2. keeps **events** as the thing that drives control flow;
3. is **real OpenTelemetry**, so any OTLP collector plugs in easily;
4. projects cleanly into a **UI** (research tree + drill-down into calls/errors).

The earlier `tracing.py` emitted two flat spans (evaluate/ablate) — a placeholder, not tracing.

## Decision
**Three planes, one instrumentation layer, correlated by id.**

| Plane | File / form | Authority | Driven by |
|---|---|---|---|
| Domain **events** | `events.jsonl` | source of truth, minimal | `replay.fold` → RunState |
| **Traces** | `spans.jsonl` (+ OTLP) | observability, rich | `tracing.Tracer` |
| UI **read model** | `readmodel.sqlite`, `trace.json`, `tree.html` | derived | `build_readmodel` + `build_trace_view` |

Events = *what was decided* (coarse, authoritative). Spans = *how execution unfolded* (fine,
timing/status/errors). They are two projections of the same activity; **no duplication** — the
event says "node 3 evaluated, metric=0.9", the span subtree says "eval took 4m12s: setup /
command(exit 0) / read_metric; here's the error if any".

### OpenTelemetry done idiomatically (and the answer to "plug any collector")
This IS standard OTel practice:
- **Instrument** with one facade (`Tracer.span`) that, when `opentelemetry-api` is importable,
  opens a genuine OTel span. Code is vendor-neutral.
- **Configure** at the edge: install the `[otel]` extra and set `OTEL_*` env
  (`OTEL_EXPORTER_OTLP_ENDPOINT=…`) → the SAME spans flow to Jaeger / Tempo / Honeycomb / any
  OTLP backend, **no code change**. `OTLP` is the universal protocol; this is its whole point.
- **No package / no config → no-op** for the bridge, and a default **custom JSONL exporter**
  still writes `spans.jsonl`. So the local-first / zero-dependency default is preserved; OTel is
  purely additive.

### Correlation
`EventStore.append` stamps the active `(trace_id, span_id)` into every event envelope; spans
carry `run_id` + `node_id`. So the UI joins the event tree to its span subtree, both directions.

### Trace topology
**One trace per top-level operation** (create_node / evaluate / ablate / confirm_seed /
onboard), tagged with `node_id`, NOT one giant per-run trace. Real runs are long and resumable;
a single in-memory trace can't survive resume and would be unbounded. The run-level tree is
reconstructed from **events** (parent_ids) — another reason events, not a trace, are the
structural source of truth.

### Determinism preserved
`replay.fold` never reads spans. Tracing can be incomplete (crash) or non-deterministic
(timings, random ids) without affecting engine state, resume, or `config_hash`.

## Coverage
Nested spans with status (OK/ERROR + recorded exception) and attributes:
`create_node` → `propose` / `implement` / `repair`; `evaluate` → `setup` / `command` /
`read_metric` (+ eval_seconds/exit/metric/drift/violations attrs); `ablate`, `confirm_seed`,
`onboard`.

## Consequences
- Default run dir now also has `spans.jsonl` and `trace.json`; `tree.html` renders the per-node
  span tree, failure reason, eval time, and infeasibility.
- `pip install autornd[otel]` + `OTEL_EXPORTER_OTLP_ENDPOINT` → live traces in any collector.
- A future React UI consumes `trace.json` (+ the SQLite read model) with no engine coupling.
