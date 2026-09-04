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

Loss is observable in three forms:

- `AsyncJsonlSpanExporter.metrics()` is a race-consistent process-local snapshot;
- coalesced `looplab.exporter.loss` internal spans durably record dropped-span and export-failure deltas
  through a direct, bounded writer path that cannot itself enter or be evicted by the ordinary queue.
- `trace_export_health` rows, appended to `events.jsonl` **by the engine**, publish that same snapshot
  whenever `trace_export_unhealthy` holds — see below for why the first two are not enough.

### A span's row must not depend on its context bookkeeping

A row is written when a span CLOSES, so an operation that loses its close writes nothing while its
children write normally. Measured on `runs/e5small-dr-unified-v12/spans.jsonl`: 3618 spans, 31
distinct parents, and **4 parent ids appear on 268 children while owning no row of their own** —
`891a4e7216bf6d` alone has 256, and it is the parent of the last six spans that run ever wrote.

The cause is an ordering inside `Tracer.span`'s `finally`, and it is driven rather than reasoned:

    finally:
        rec["duration_s"] = ...
        otel_cm.__exit__(...)
        _stack.reset(token)            ─┐  every one of these is a ContextVar.reset, which RAISES
        _current_tracer.reset(...)      │  ValueError("... was created in a different Context")
        _node_ctx.reset(...)            ├─ when a span's enter and exit land in DIFFERENT contexts
        _generation_ctx.reset(...)      │
        _phase_ctx.reset(...)          ─┘
        try: self.exporter.export(rec)  <- was LAST: one raising reset skipped it and the span vanished
        except: pass

Entering under `contextvars.copy_context().run(cm.__enter__)` and exiting outside it reproduces it
in three lines: the exit raises `ValueError`, and no row is written.

The fix is the order — export first, then unwind:

    finally:
        rec["duration_s"] = ...
        otel_cm.__exit__(...)
        try: self.exporter.export(rec)   <- the diagnostic is recorded before anything can raise
        except: pass
        _stack.reset(token) ... etc      <- bookkeeping, still allowed to raise and propagate

The export stays wrapped, so a failing exporter still cannot mask the in-flight exception; what
changes is that context bookkeeping no longer decides whether a diagnostic gets recorded. The
`ValueError` still propagates — it is real information about a span that crossed a context
boundary — and `tests/test_span_survives_a_failed_context_reset.py` pins both halves.

THIS IS NOT THE WHOLE OF v12's OUTAGE and the tests say so: the tracer SURVIVES a failed reset, a
later span records normally, so a lost close does not by itself explain a run that stopped writing
spans for 33 hours. It explains the four orphans, which is what it claims.

### A dead exporter cannot report that it is dead

Both loss surfaces above are written BY the exporter. The durable receipt is appended from
`_worker_main`, and the `_LOG.warning` beside it is raised on the same path, so an exporter whose
worker has stopped emits neither. `metrics()` survives the worker, but nothing in the product read it.

MEASURED on `e5small-dr-unified-v12`, 2026-09-01: `spans.jsonl` last written 18:20, `events.jsonl`
still appending 10.5 hours later, `py-spy dump` showing no `looplab-trace-export-*` thread in the live
engine, and zero loss receipts in the run. The outage was total, ongoing, and completely silent.

    span ends ──▶ exporter queue ──▶ worker ──▶ spans.jsonl
                                       │
                                       ├──▶ loss receipt (spans.jsonl)   ─┐ both die WITH
                                       └──▶ _LOG.warning                 ─┘ the worker
                                       ·
                       metrics() ──────┴──▶ (survives the worker)
                                              │
    run loop turn ────────────────────────────┴──▶ trace_export_unhealthy?
                                                     │ no  ──▶ nothing appended
                                                     │ yes ──▶ engine appends
                                                               trace_export_health
                                                               to events.jsonl

The engine is the one writer that outlives the exporter, so `Engine._record_trace_export_health` reads
the snapshot once per turn and publishes it on the run's own log. The row is DIAGNOSTIC (invariant #1:
`_proposal_authority_seq` excludes `DIAGNOSTIC_EVENTS` wholesale, so it cannot displace a paid
proposal), it is gated on `trace_export_unhealthy` so a healthy run's log is untouched, and it is
deduplicated on the snapshot itself so a permanently-dead exporter costs one row per distinct state
rather than one per turn. It is published BEFORE the turn's decision prefix is read, so the row is part
of the fold that turn reasons over and cannot move the tail under the sequence recheck that follows.

`trace_export_unhealthy` fires on FOUR independent symptoms. Three were there from the start:
`shutdown` (the exporter stopped
accepting for good), a dead worker with rows still QUEUED (an idle exporter with an empty queue
legitimately owns no thread), or any recorded loss — a drop, an export failure, or a loss receipt that
itself failed to write.

The fourth landed with `TRACE_WORKER_STOP_REASONS` and closes a hole this predicate had from the
day it shipped: **a worker that died for a harmful reason, with nothing dropped**. Before the
registry the five terminal paths were byte-identical from the outside, so a CRASHED worker with an
empty queue and zero drops looked exactly like one resting between submits — and that is v12's
shape: no receipts, no drops, no thread, and no row. The split is a denylist, not an allowlist:

    routine, no row      idle       parked with nothing queued; the next submit restarts it
                         retired    handed the file off; the next submit restarts it

    spans are lost       crashed          an exception escaped the worker loop
                         receipt_failed   the loss receipt itself could not be written
                         abandoned        terminal ownership released — no more spans from here

A denylist because a SIXTH reason added upstream without touching this set reads as routine, which
is the safe direction for a diagnostic that must not cry wolf; and
`test_the_denylist_names_only_reasons_the_exporter_can_produce` refuses a word the exporter cannot
emit, so the set cannot rot into a decoy.

Together the two halves make the outage reportable: the registry names WHY the worker stopped,
`metrics()` carries it (`worker_stop_reason`, `worker_stop_detail`, and a counter per reason), and
this row publishes it on the run's own log — which the exporter itself could never do.
The receipt receives one delegate attempt as well: an exception may happen after its append committed,
so retrying the same delta could inflate every postmortem count. On ambiguous failure the process-local
snapshot remains authoritative for that process, while the durable summary may undercount but never
double-count that delta.

`trace.json.summary` sums those receipts into `dropped_spans`, `export_failures`, and
`exporter_loss_receipts`. `exporter_metrics_partial=true` means the bounded trace tail omitted older spans,
so the visible delta sum is a lower bound rather than a complete postmortem count. A terminal shutdown
timeout deliberately does not append a late loss receipt after lifecycle ownership is released; its
process-local `dropped_shutdown_timeout` counter remains available to the owner that observed the timeout.

`force_flush` is a reusable barrier over accepted work and `shutdown` is a one-shot bounded wait. Their
timeout limits only how long the caller waits; Python cannot interrupt a worker already inside filesystem
I/O. **State the barrier exactly, because two nearby claims are false.** It settles every row accepted
before or during the call — the wait clears only on an empty queue, no active row AND a retired worker, so
a row already handed to the writer holds the barrier just as a waiting one does. What it settles is the one
delegate ATTEMPT, not its success: an accepted row whose attempt raised is permanently absent from
`spans.jsonl`, and `force_flush` still returns `True`, because that boolean reports whether the LOSS
RECEIPT failed. So a reader behind the barrier sees every span that was written plus a
`looplab.exporter.loss` row counting the ones that were not — never a silent hole — and a caller wanting
"the artifact contains every span I queued" must read `export_failures` / the receipt rather than the
return value.
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
descriptor. It rotates on replacement/rewrite but remains stable across a receipt-proven append
chain, so a node/window revision can be computed from selected-row membership and digests without
re-reading the heavy rows. An append for another node therefore leaves this revision stable on that
fast path, while a selected append, in-place rewrite, replacement, attempt or window change invalidates
it. Schema-2 append receipts carry that same descriptor-bound mutation token on BOTH sides of the
append, so the chain proves "the prefix was not rewritten" on Windows too, where `st_ctime_ns` is a
creation stamp and could never witness it. Windows requires that proof: a schema-1 receipt, or one
whose writer could not read the token, is refused and the observation rebuilds — the conservative
answer that platform previously gave to ALL growth. Both receipt versions stay readable, with an exact
key set per version. If ChangeTime itself is unavailable, every observation rebuilds with a volatile
revision and neither warm nor persisted validators are reused. Built incrementally where the mutation proof is complete (mirrors `EventStore`'s incremental
read), persisted atomically, and STRICTLY an
accelerator: any identity/size/corruption mismatch rebuilds from `spans.jsonl`, producing the same safe
projection as the un-indexed path — never a second source of truth, worst case as slow as before. (Index/payload
separation + byte-offset seeks is the Grafana-Tempo / Jaeger / Perfetto pattern; JSONL + orjson is
kept over SQLite/Arrow deliberately — no locking, atomic-rename-safe on the FUSE/NFS/S3 mounts the
rest of the store already guards for.)

Full-row windows coalesce selected rows separated by at most 256 KiB into continuous reads capped at
8 MiB. This targets the S3/FUSE cost boundary: the previous reader issued one seek/read (normally one
range GET) per selected span, while S3 cannot return disjoint ranges in one GET. Gap bytes are never
parsed or returned, and every selected slice still passes its exact row digest plus normalized-light
comparison. `tools/bench_trace_s3_reads.py` drives the production planner across dense, 4-way and
32-way-interleaved layouts; the 256 KiB threshold captures the large request-count win in a moderately
interleaved trace without the ~30× byte amplification a 1 MiB/single-cover strategy creates in a
highly interleaved one.

### Browser projection boundary

`spans.jsonl` is diagnostic files-as-truth, but it is not a trusted HTTP payload. Custom exporters,
old runs and hand-edited files can contain unknown objects, credentials or pathological sizes. Every
trace, node-detail, tail, operation and conversation reader therefore passes span material through the
same versioned allowlist projector before data enters the persisted index or browser:

- span/attribute/event fields, collection sizes, nesting depth, text and the shared per-span text budget
  are capped; the response's SPAN window is the one knob for how much of a node is read, and the
  stage/turn render caps are derived from it as its own arithmetic bound (`conversation_render_caps`:
  at most one band and two turns per span, since neither can exist without one). They were flat
  numbers until 2026-08-13, which made them a second, invisible ceiling: on `runs/rubert-dr-0804`
  node 1 a 512-span window derived 256 bands and 425 turns and rendered 64 and 105 — evidence the
  server had already paid 3.4 ms/span to read and 0.9 ms/span to thread, then dropped. Lifting them
  at a fixed window is 0 ms (measured: `build_conversation` 0.17 s vs 0.18 s at that window) and
  costs only response bytes, now honestly proportional to the window asked for;
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
| `trace/by_trace/{trace_id}/conversation` | the same stage/turn omission counters as the node conversation — it is the same projection over one operation's spans instead of one node's |
| `spans/{span_id}` | `detail_truncated` for the selected span plus `siblings_elided`, `trace_total_spans`, `trace_visible_spans` and `omitted_trace_spans` when cardinality is known |
| `trace/tail` | the bounded tail's visible/omitted counts and `source_truncated`; it does not pretend to know a whole-run total |

The two per-node routes also take `?before=<span_id>`, which MOVES that window instead of growing it:
the same `limit` spans ending at the anchored step rather than at the node's newest one. It exists
because the window is a TAIL and widening is therefore the same tail extended — on the node above,
14,507 spans over 3 h 50 m, the default window covers the last 7.6 minutes and the ceiling the last
59.3, so 74 % of it was unreachable at any limit. `GET /nodes/{n}/episodes` names the places worth
anchoring at: every band the conversation reads, with none of their contents, each carrying its
`anchor`. It is the SAME band derivation the conversation uses (`_conversation_bands`) and is served
from the in-memory light index without touching `spans.jsonl` — 7,048 episodes in 82 ms on that node.
The map response is capped at 10,000 rows but the cap is now a page size, not a history wall:
`?before=<first-visible-anchor>` is an exclusive episode cursor, and the browser prepends older pages
until `has_older` is false. Every page echoes the initial tail's inclusive `snapshot` anchor and the
browser sends it on the next request, so normal live appends cannot move a backward walk. Cursor and
snapshot are validated against the selected node and lifecycle's derived
bands (not just any span in the run); a stale/foreign cursor is refused with 409
`trace_episode_cursor_unknown`. Thus a node with more than 10,000 bands can still reach its first
episode while every response remains bounded.
An anchor the index cannot place is refused (409 `trace_anchor_unknown`), never degraded to the tail,
and it is material in both the index's window revision and the route's ETag so a conditional read can
never answer one anchor with another's body.

Both `by_trace` routes echo the requested `trace_id` and take the same `limit` window the node routes
take (settled by `settle_node_span_cap`, so the ceiling stays `TRACE_NODE_SPAN_CAP_MAX`). Neither is
decoration: the browser fences a late response against the subject it asked for, and the span route's
256-span default really binds on a Researcher proposal (measured 2026-08-12: 252 spans on
`runs/rubertlite-dr-unified-v5` card-0, 272 on the v3 backup's), so the one trace surface that renders
both nodes and operations would otherwise meet a wall on one subject that it lifts for the other.

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

### Run-scoped work a node claims (`build_trace`)
Some top-level operations legitimately have **no node to be tagged with**, because they run before
one exists. `propose` is the oldest case; under `card_driven_selection` the whole Developer build is
another — `card_build` opens on a speculative producer worker before any node id is reserved, and the
id it could compute is a *prediction* that `_claim_requested_card_build` re-derives after that span
has closed (the build may be refused and mint no node at all).

Measured on `runs/rubertlite-dr-unified-v7` (2026-08-14): 2,403 of 2,637 spans (91.1 %) were
attributable to no node, 1,312 of them the three `card_build` traces — `plan`, `stages` and the whole
implement loop. The serial-path run `rubert-dr-0804` has nine such spans out of 14,846 (0.1 %),
because `_create_node` opens `create_node` with the node id and the build runs inside it.

So the pointer runs the other way. `card_build` carries the request it serves (`card_id`,
`card_build_generation`) and no node; the **node** stamps `build_trace` on its own
`materialize_node` span once it commits, which is the first moment both facts exist together.
`events/traceview.py::claimed_build_traces` is the one reading of that claim, shared by
`span_index._rows_for_node`, `_bounded_node_trace_tail`, `_conversation_bands`, `build_trace_view`
and `project_card_trace`. Three rules hold it honest: a claim only ever fills a trace that names no
node of its own, a span may not claim its own trace, and a trace two nodes claim is awarded to
neither. The claim carries the *claiming* span's lifecycle, so a `node_reset` that rebuilds reaches
its own build and not the abandoned one.

`propose` deliberately keeps NO such claim: a card's research belongs to every node the card carries,
not to whichever one was prepared first (`orchestrator.stamp_proposal_span`), and it is reachable
through the card trace (`/api/runs/{run}/cards/{card}/trace`). `looplab timings` also does not follow
the claim — "who owns this wall clock" must keep charging a producer turn to the run.

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


## A failure that does not name itself is undiagnosable from inside the product

MEASURED on `runs/e5small-dr-unified-v13`, live. The exporter froze at `exported_spans: 3970` and
failed every export after it:

| +min | accepted | exported | export_failures | loss_receipt_failures | worker_alive |
|---|---|---|---|---|---|
| 233.8 | 5154 | **3970** | 1184 | 858 | true |
| 296.5 | 6376 | **3970** | 2406 | 1691 | false |
| 296.6 | 6386 | **3970** | 2416 | 1695 | true |
| 322.2 | 7419 | **3970** | 3449 | 2246 | false |

`spans.jsonl` and `.spans-append.jsonl` both stopped at the same instant; the run produced spans for
three more hours. The loop is `export fails → write a loss receipt → the receipt fails →
_retire_worker_locked("receipt_failed") → the next span restarts the worker → identical failure`,
which is the `worker_alive` flapping while both counters climb.

SIX CAUSES had to be eliminated from OUTSIDE the process, against the live frozen file — writability
(an `O_APPEND` open succeeds), ENOSPC (1 PB at 0%), a stale descriptor (`/proc/<pid>/fd` holds
none), a held flock (`LOCK_NB` acquires immediately), descriptor/path divergence (`fstat == stat`,
inode stable — the exact post-yield validation the helper performs), and a torn tail (the last
complete line is 41,646 bytes of valid JSON). None is the cause.

The seventh could not be reached. The console log carried the failure 3,449 times and said only
`trace export lost spans: none (export failures: 1)`, because the delegate's exception was
discarded by two `except Exception: pass` handlers. **Retaining the delta for a later attempt is
right; discarding the reason is what made the class undiagnosable.**

```
  THE TWO SWALLOW SITES, and what each now records

   worker loop
     ├─ per-span export ──► _writer._export_line(item)
     │      except Exception as exc ──► export_error = _bounded_export_error("export", exc)
     │                                        │   phase-prefixed: the two writers differ and so
     │                                        │   do their remedies
     │      with self._condition: ────────────┘   recorded under the SAME lock as the counter it
     │          self._last_export_error = ...     explains, so the health row reads them together
     │
     └─ loss receipt ─────► _writer._export_line(receipt_line)
            except Exception as exc ──► receipt_error = _bounded_export_error("receipt", exc)
            failure ──► _retire_worker_locked("receipt_failed")

   metrics() ──► {..., export_failures, last_export_error, worker_stop_reason, ...}
                                            │
   engine ──► trace_export_health row ──────┘   and the WARNING line now names it too

   Bounded at 240 chars: an unbounded field on a row emitted once per failure is a second loss
   mechanism. Empty means nothing has failed in THIS process — it resets in `_reset_process_state`
   with the counters, because an error carried across a fork would blame a child for its parent.
```
