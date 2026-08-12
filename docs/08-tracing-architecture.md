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

**Per-run LLM-I/O capture permission** (ADR-17): whether a run persists its (bounded, redacted)
prompt/completion/tool-argument text is that RUN's decision (`Settings.trace_llm_io`), so it is bound
to the run's own `Tracer` (`Tracer(..., capture_llm_io=…)`, wired by the Engine) and scoped by
`Tracer.span` through a contextvar for the span's lifetime. Several tracers are live in one process —
the UI's Assistant, Genesis, a library caller driving two `Engine`s — and with the permission held in
a single module global the last starter decided for all of them, either persisting an opted-out run's
prompts or silently dropping an opted-in run's. The module global survives as the PROCESS-WIDE DEFAULT
(`tracing.set_llm_capture`, what the CLI calls) for tracers that declare nothing. An observation
(`generation` / `tool`) binds the effective policy when it is CREATED, inside its own span: a streamed
generation attaches its output across suspensions of a generator that runs in the *consumer's* context,
which may by then be another run's.

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

### Bounded local export and lifecycle fence

The Engine puts a bounded asynchronous processor in front of the local JSONL writer. Closing a span
serializes it once and submits it without waiting for filesystem I/O; the prepared row is reused by the
worker, so there is no second JSON dump or mutable-record copy. Caller-side serialization is the explicit
tradeoff that makes byte accounting exact: I/O moves off the observed path, while serialization remains
synchronous and bounded by the physical 8 MiB row contract.

The queue admits at most 256 **waiting** rows and 16 MiB of prepared bytes. The count excludes the one row
currently owned by the worker, while the byte charge includes it. `queue_full` therefore means the waiting
row count is saturated; `queue_bytes` means the new prepared row does not fit the remaining byte budget.
Either condition drops the newest span and returns `False`; accepted rows remain FIFO and receive exactly
one delegate attempt, because retrying an exception after an ambiguous append could double-export. One
worker is reused across sporadic submits and is retired by an explicit flush/shutdown (or a long calm idle
period), so a long run neither creates a thread per span nor retains unbounded daemon workers.

Loss is observable in two forms:

- `AsyncJsonlSpanExporter.metrics()` is a race-consistent process-local snapshot;
- coalesced `looplab.exporter.loss` internal spans durably record dropped-span and export-failure deltas
  through a direct, bounded writer path that cannot itself enter or be evicted by the ordinary queue.
The receipt receives one delegate attempt as well: an exception may happen after its append committed,
so retrying the same delta could inflate every postmortem count. On ambiguous failure the process-local
snapshot remains authoritative for that process, while the durable summary may undercount but never
double-count that delta.

`trace.json.summary` sums those receipts into `dropped_spans`, `export_failures`, and
`exporter_loss_receipts`. `exporter_metrics_partial=true` means the bounded trace tail omitted older spans,
so the visible delta sum is a lower bound rather than a complete postmortem count. A terminal shutdown
timeout deliberately does not append a late loss receipt after lifecycle ownership is released; its
process-local `dropped_shutdown_timeout` counter remains available to the owner that observed the timeout.

`force_flush` is a reusable visibility barrier and `shutdown` is a one-shot bounded wait. Their timeout
limits only how long the caller waits; Python cannot interrupt a worker already inside filesystem I/O.
Finalization flushes immediately before reading `spans.jsonl` for `trace.json`/`tree.html`, and
`Engine.run()` always performs one bounded terminal shutdown in `finally` for success, error,
cancellation, pause and abort paths. A span that closes after `run()` returns is rejected. If shutdown
times out, pending rows are atomically retired and an active row must revalidate ownership after the
canonical writer fence before commit. Post-shutdown misuse is counted only in the process-local snapshot;
it never resurrects a worker merely to append a late durable receipt.

The Engine explicitly constructs its exporter with `lifecycle_fence=True`; filenames do not grant this
capability. That exporter and destructive clear/reset/archive/delete paths share `.spans-writer.lock`.
Acquiring this sidecar is fail-closed and precedes opening the source, while the historical data-file lock
for custom exporters remains best-effort. Normal live index refresh stays on `.spans-index.lock`, so a cold
large-index rebuild cannot backpressure the exporter. A writer that crossed the fence first completes
before a destructive rewrite; a timed-out writer still waiting at the fence is abandoned and cannot append
afterward. Default/custom exporters—even when their basename is `spans.jsonl`—do not create the lifecycle
sidecar. After `fork()`, the child creates fresh synchronization, discards parent-owned queued work, and
retires inherited worker descriptors without flushing copied Python buffers (an exact raw `FileIO` is
closed directly; opaque wrappers are descriptor-tombstoned and quarantined); only child-owned spans may
be submitted in the child.

**Light span index** (`events/span_index.py`): even delta-encoded, `spans.jsonl` still carries heavy
generation I/O (prompt/output/reasoning), so a long run's file is large and parsing it whole made the
UI's first trace click stall ~15 s. The index keeps a
~25×-smaller, versioned and bounded/redacted projection of every span plus
the byte `(offset,length)` of the full span in `spans.jsonl` — so the timeline reads only the tiny
index and per-node/-span detail views seek to exact offsets. Each index row also carries the digest of
its exact full source bytes; every full-row seek rehashes those bytes and reports a mismatch as
unavailable rather than silently returning altered/incomplete evidence. A persisted source epoch is
bound to POSIX `st_ctime_ns` or Windows `FILE_BASIC_INFO.ChangeTime` read from the already-open source
descriptor. It rotates on replacement/rewrite but remains stable across a receipt-proven POSIX append
chain, so a node/window revision can be computed from selected-row membership and digests without
re-reading the heavy rows. An append for another node therefore leaves this revision stable on that
fast path, while a selected append, in-place rewrite, replacement, attempt or window change invalidates
it. Windows append receipts currently contain creation time rather than ChangeTime, so Windows growth
conservatively rebuilds instead of incrementally trusting them; if ChangeTime itself is unavailable,
every observation rebuilds with a volatile revision and neither warm nor persisted validators are
reused. Built incrementally where the mutation proof is complete (mirrors `EventStore`'s incremental
read), persisted atomically, and STRICTLY an
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
| `spans/{span_id}` | `detail_truncated` for the selected span plus `siblings_elided`, `trace_total_spans`, `trace_visible_spans` and `omitted_trace_spans` when cardinality is known |
| `trace/tail` | the bounded tail's visible/omitted counts and `source_truncated`; it does not pretend to know a whole-run total |

The span-detail route retains aggregate `truncated` for generic tree/tail consumers, where either a
bounded selected span or omitted trace siblings makes the envelope partial. A selected-detail notice
must use only `detail_truncated`; `siblings_elided` is not evidence that the chosen span's I/O was cut.

`trace/tail` is a separate best-effort EOF window for live activity: it may skip malformed rows inside
that bounded window, and its receipt therefore describes only the inspected tail rather than forward-log
durability or whole-run cardinality.

The node-conversation endpoint exposes that selected revision as an opaque weak `ETag` and as the
additive JSON `cursor` on a stable 200. Its representation identity also includes projection schema,
URL and trace run/task identities, run generation, node attempt, and the settled span window. A matching
`If-None-Match` returns an empty 304 without reading full rows or rebuilding the conversation, but only
after re-observing the selected source revision and the generation/attempt/reset fences. A 200 assembled
on the slow path repeats both comparisons after projection; if the source moved, the raced body remains
usable but has no cursor/ETag and cannot validate a later poll.

A cold-loaded persisted index is not eligible to authorize that 304 by itself. The first conditional
read in a process is forced through a 200 source-row read; exact row digests and normalized light metadata
must verify before that node/window revision is promoted for later bodyless polls. The persisted index is
a private same-user accelerator, not an authenticated manifest: deliberate standalone edits that remove
otherwise valid index membership without changing `spans.jsonl` are outside this cache-integrity boundary
and would require either a full source scan or an authentication root stored outside the index.

Conversation diagnostics intentionally use `Cache-Control: no-store`. The Inspector holds only its
same-scope last-good payload in component memory and manually sends its ETag; neither browser nor shared
HTTP caches retain prompt/tool diagnostics. A 304 is a tagged transport outcome, never parsed as JSON.
Reuse requires the same run, generation, node, attempt and span window plus an identical response ETag;
a first, missing-tag or mismatched-tag 304 is recovered with one unconditional read under the same
AbortSignal/deadline. Thus a refresh failure keeps the last confirmed view visibly stale, while an
unmount, deadline or lifecycle change cannot commit or borrow another scope's payload.

### Browser span-tree scale and lifecycle

The server's tree builder and final JSON encoder are both iterative: the node, run and operation
trace routes return explicit JSON responses instead of sending a nested forest through FastAPI's
recursive encoder, and finalization writes `trace.json` through the same encoder. The existing span
and field caps bound source material; a final 64 MiB byte ceiling bounds the aggregate document
without imposing a depth cap or changing the nested wire shape.

Node Trace, operation Trace and the raw Inspector share one flattened, variable-height virtual tree.
The server-returned forest remains logically complete — there is no per-sibling cap or recursive
"show all" path — while only the viewport, overscan and at most one far active descendant are mounted.
Flattening, bounds and roll-ups are iterative, so a deeply nested custom trace does not consume the
JavaScript call stack. The renderer reuses the Timeline windowing primitive and adds no client runtime
dependency. The major topology/search/accessibility contract was measured at 508,941 B total JS gzip
and 355,333 B for the owner DAG; only those two ceilings were rebased to 498/348 KiB, leaving about
1 KiB headroom while all per-chunk, lazy-route, forbidden-reachability and CSS gates remain unchanged.

The tree follows these interaction and lifecycle invariants:

- the top-level stage is the real root span, so its bounded attributes and events remain disclosable
  even when it has children; this preserves legacy root `llm_call` evidence;
- one `role="tree"` keyboard stop owns selection. Up/Down and Home/End move through the flattened
  topology, Left/Right move to parent/first child, and Enter/Space toggle the active observation's
  detail. `aria-level`, sibling position/size and a mounted `aria-activedescendant` describe the full
  logical topology without making every disclosure a separate tab stop;
- search covers every logical row plus a bounded allowlist of scalar attribute/event metadata
  (identity, status, model/tool/stage, reason/error and small counters). Unknown or nested fields are
  excluded; captured prompt, messages, input/output, thinking, completion and tool-call payloads are
  never serialized into the index;
- expanded observations, fetched bounded detail and nested reasoning disclosure are keyed by stable
  span identity above the virtual rows. They survive viewport unmount/remount and same-scope polling;
  rows evicted from a live fixed window are pruned immediately, and a generation/node/attempt reset
  remounts the state owner so stale detail cannot cross a 409 identity fence;
- an Agent validation report is node-level evidence, not a span, and therefore follows the complete
  span tree instead of being presented as a child of whichever author-stage row happens to be mounted.

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
  (`OTEL_TRACES_EXPORTER=otlp` and/or `OTEL_EXPORTER_OTLP(_TRACES)_ENDPOINT=…`) → the SAME spans
  flow to Jaeger / Tempo / Honeycomb / any OTLP backend, **no code change**. `OTLP` is the universal
  protocol; this is its whole point. A different `OTEL_TRACES_EXPORTER` is not silently replaced with
  OTLP, and `OTEL_SDK_DISABLED=true` is an explicit off switch.
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
- `pip install LoopLab[otel]` + explicit OTLP exporter/endpoint env → live traces in any collector.
- The React Inspector consumes only bounded HTTP projections derived from events/spans, with no
  engine coupling and with explicit partial/unavailable states. `readmodel.sqlite`, `trace.json`, and
  `tree.html` remain rebuildable derived artifacts rather than an Inspector data dependency.
- Browser span forests use one topology-complete logical tree and a globally bounded DOM window;
  disclosure/detail state is retained only for span ids still present in the current projection.
