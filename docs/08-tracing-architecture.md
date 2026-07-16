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
| Domain **events** | `events.jsonl` | replay authority for `RunState`, minimal | `replay.fold` → RunState |
| **Traces** | `spans.jsonl` (+ OTLP) | observability, rich | `tracing.Tracer` |
| UI **read model** | `readmodel.sqlite`, `trace.json`, `tree.html` | derived | `build_readmodel` + `build_trace_view` |
| Trace **index** | `spans.index.jsonl` | derived cache (accelerator) | `events.span_index` |

**Delta-encoded generation input** (`tracing.generation`): the agent tool-loop re-sends the WHOLE
growing conversation on every turn, so storing each generation's full `input` made ~90 % of
`spans.jsonl` a re-send of the same messages. When a generation STRICTLY EXTENDS the prior one in its
trace (a tool-loop turn that only appended to the same conversation — the prior input is a full leading
prefix of this one), it stores only the appended tail plus a back-ref — `input` (the delta) +
`input_carry` (carried-prefix count) + `input_from` (prior span_id); a context reset / new sub-loop
(propose→implement→repair, whose history diverged, not merely grew) stores a full self-contained base
(`input_carry == 0`, `input_from = None`). This shrinks `spans.jsonl` ~6×
at the source (one append-only file, no separate blob store). Within the safe projection pipeline, the
trace reader reconstructs the complete **retained diagnostic input** when its chain is present
(`traceview.hydrate_inputs`) and marks an incomplete chain `input_partial`. This is not a promise of
byte-exact provider I/O. `build_conversation` needs no reconstruction (it
treats `input_carry == 0` as the sub-loop request boundary and shows that base's full initial context
once, then each generation's delta). Old logs (no `input_carry`)
are read unchanged. Correctness never depends on the write-time chain surviving thread/task hops — a
stale chain just resets to a full base (less compression, never wrong).

**Light span index** (`events/span_index.py`): even delta-encoded, `spans.jsonl` still carries heavy
generation I/O (prompt/output/reasoning), so a long run's file is large and parsing it whole made the
UI's first trace click stall ~15 s. The index keeps a
~25×-smaller, versioned and bounded/redacted projection of every span plus
the byte `(offset,length)` of the full span in `spans.jsonl` — so the timeline reads only the tiny
index and per-node/-span detail views seek to exact offsets. Built incrementally (parse only the
appended tail; mirrors `EventStore`'s incremental read), persisted atomically, and STRICTLY an
accelerator: any identity/size/corruption mismatch rebuilds from `spans.jsonl`, producing the same safe
projection as the un-indexed path — never a second source of truth, worst case as slow as before. (Index/payload
separation + byte-offset seeks is the Grafana-Tempo / Jaeger / Perfetto pattern; JSONL + orjson is
kept over SQLite/Arrow deliberately — no locking, atomic-rename-safe on the FUSE/NFS/S3 mounts the
rest of the store already guards for.)

### Browser projection boundary

`spans.jsonl` is diagnostic files-as-truth, but it is not a trusted HTTP payload. Custom exporters,
old runs and hand-edited files can contain unknown objects, credentials or pathological sizes. Every
trace, node-detail, tail, operation and conversation reader therefore passes span material through the
same versioned allowlist projector before data enters the persisted index or browser:

- span/attribute/event fields, collection sizes, nesting depth, text and the shared per-span text budget
  are capped; response span/stage/turn counts are capped independently;
- persisted text is redacted before it is returned, nested secret-named fields are masked, and a
  secret-shaped required identity is quarantined instead of rewritten into a different topology;
- complete JSON-object rows with an invalid span shape are quarantined individually; an invalid-JSON,
  non-object, or torn forward row remains a durability boundary rather than being guessed past;
- every successful response carries a route-appropriate `projection` receipt, and each truncated span
  carries its own `_projection` counters. The receipt fields are intentionally not uniform because a
  one-span seek, a bounded file tail and a run tree know different source totals.

The HTTP envelopes use projection schema 2, but consumers must interpret the receipt for the route they
called rather than assume that every response has `total_spans` and `visible_spans`:

| Route family | Success receipt |
|---|---|
| run/node trace and node conversation | known total/visible/omitted span counts plus truncated-span or stage/turn omission counters |
| `trace/by_trace/{trace_id}` | operation `count`, `visible_count`, `omitted_count` plus the corresponding span projection receipt |
| `spans/{span_id}` | the selected span's `_projection` counters plus `trace_total_spans`, `trace_visible_spans` and `omitted_trace_spans` when known |
| `trace/tail` | the bounded tail's visible/omitted counts and `source_truncated`; it does not pretend to know a whole-run total |

`trace/tail` is a separate best-effort EOF window for live activity: it may skip malformed rows inside
that bounded window, and its receipt therefore describes only the inspected tail rather than forward-log
durability or whole-run cardinality.

A read failure is different from a successful empty projection. It returns the route's empty collection
shape with top-level `schema: 2` and `projection: {schema: 2, unavailable: true, truncated: true}`; unknown
counts are omitted rather than fabricated as zero. Collection readers treat an absent `spans.jsonl` as a
known complete-empty source and may report exact zeroes; a lookup for a particular absent span remains
unavailable.

Raw full diagnostics remain confined to the run-root `spans.jsonl` family; neither the trace API nor the
generic Artifact browser exposes trace sources, derived views, archives, or atomic temporaries. The
Inspector and live Dock distinguish unavailable, partial and honestly empty projections instead of
silently presenting a failed or capped read as complete. If the server cannot prove that an artifact is
independent of those protected files (including aliases and reserved directories), artifact access is
unavailable rather than a successful empty inventory.

Events = *what was decided* (coarse, authoritative for replay). Spans = *how execution unfolded* (fine,
timing/status/errors). They are complementary records of the same activity; limited correlation fields
overlap intentionally, but neither can reconstruct the other — the
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
structural replay authority.

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
- `pip install LoopLab[otel]` + `OTEL_EXPORTER_OTLP_ENDPOINT` → live traces in any collector.
- The React Inspector consumes only bounded HTTP projections derived from events/spans, with no
  engine coupling and with explicit partial/unavailable states. `readmodel.sqlite`, `trace.json`, and
  `tree.html` remain rebuildable derived artifacts rather than an Inspector data dependency.
