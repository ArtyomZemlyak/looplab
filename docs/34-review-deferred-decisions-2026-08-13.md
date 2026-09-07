# 34 — Deferred decisions from the two-round review (2026-08-13)

An 18-angle review of `2c36b7e..HEAD` produced ~90 findings. Most were fixed in the same change
(see `git log` for `fix: correctness and doc-sync findings…`, `fix: security, cost, contract and
test-integrity…`, `refactor: reuse, budget and hot-path findings…`). Seventeen of thirty-two
correctness candidates were REFUTED under adversarial verification and are recorded there.

This document holds the residue: findings that are **real and reproducible but whose fix is a
product or architecture decision**, not a defect repair. Each was supposed to have a pointer comment
at its site so a reader meets the reasoning where the cost is paid, rather than only here — **and
only three of the five do** (re-derived 2026-08-19: `grep -c 'docs/34'` is 0 in both
`core/tracing.py` and `ui/test/settingsSchemaResource.test.js`). D-02's own status update admits it
while this paragraph asserted the opposite; `docs/00-INDEX.md` propagates the false version.

Nothing below is a bug you can fix without deciding something first. That is the entry criterion.

> **Re-derived 2026-08-19 against master `8be301f7`, and INDEXED.** Four of the five are genuinely
> still deferred and now carry `OPEN[…]` markers whose proofs the guard re-derives from the tree;
> **D-05 is CLOSED** and its own status update was false the day it was written. From here,
> `grep -rn 'OPEN\['` is the answer to "what is still deferred in doc 34?".
>
> - **OPEN[agent-node-purge-has-no-durable-receipt]** `_purge_node_snapshot` is still an irreversible
>   multi-file transaction inside a bare `try/finally` with the ad-hoc `events.jsonl.bak-del<N>`
>   backup, still reachable from the agent-facing tool provider — no operation id, no phase, no
>   crash-recovery record. proof:present:_purge_node_snapshot@looplab/tools/machine_runs_tools.py
>   (re-pointed 2026-08-25: the previous literal, the backup filename, exists in that file only
>   inside the f-strings that BUILD the path, so `test_no_proof_is_satisfiable_only_by_prose`
>   rightly refused it and the guard sat red from the day this row was indexed — the exact
>   silent-red this index exists to end. The deciding symbol is the function itself.)
> - **OPEN[trace-exporter-hardens-per-span-not-per-batch]** the worker still takes exactly one row per
>   iteration and runs the whole hardened ladder (guarded open, torn-tail heal, identity CAS before
>   and after, one append receipt) per SPAN. proof:`line:item: Optional[bytes]&&= None@looplab/core/tracing.py`
> - **OPEN[card-trace-scans-whole-run-span-index]** `card_trace_view` still copies the whole run's
>   light spans; `SpanIndex` has no `card_id` dimension and `_SCHEMA` is unbumped.
>   proof:`line:_SCHEMA&&= 12@looplab/events/span_index.py`
> - **OPEN[span-index-hashes-every-source-row]** `_read_full` still re-hashes each selected row's FULL
>   bytes on the request path, and a 64-char digest is retained per row in memory and on disk.
>   proof:present:hashlib.sha256(data).hexdigest()@looplab/events/span_index.py
>
> **Dead line citations, all three in the 2026-08-14 status updates and all off by 80-170 lines:**
> `machine_runs_tools.py:1623` → the pointer is at `:1796`; `serve/appstate.py:613` → the comment is
> at `:706`; `events/span_index.py:694` → the comment is at `:756`. `docs/00-INDEX.md` also describes
> "the settings tripwire that has drifted four times"; its own ledger records eleven.



---

## D-01 · The agent-facing node purge has no durable receipt · `tools/machine_runs_tools.py`

> **Status update (2026-08-14).** Unchanged. `_purge_node_snapshot` is still the bare `try/finally`
> multi-file transaction with the ad-hoc `events.jsonl.bak-del<N>` backup, still reachable from the
> agent-facing tool provider; the pointer comment is in place (`machine_runs_tools.py:1623`). The
> recommendation stands: make node purge operator-initiated and route it through the existing
> deletion transaction (`serve/durable_op.py` + `deletion_transaction.py`), rather than growing a
> fourth receipt protocol for a caller that has no operator to hand an ambiguous outcome to.

**What it is.** `_purge_node_snapshot` performs an irreversible multi-file transaction — rewrite the
authoritative `events.jsonl` with renumbered `seq`, replace `spans.jsonl`, delete
`spans.index.jsonl` and the append journal, `rmtree` node workdirs — inside a bare `try/finally`,
from an agent-facing tool provider.

**Why it matters.** All three sibling destructive operations go through
`serve/durable_op.py::ReceiptProtocol`: whole-run reset, whole-run deletion, and the node trace
clear. Each keeps an operation id, a phase lattice and a crash-recovery record, precisely so a
process death mid-flight is resumable or quarantinable. This path keeps none. A death between
`atomic_write_text(events.jsonl)` and `publish_prepared_snapshot` leaves a renumbered event log
whose `seq` values no longer match the unfiltered spans sidecar, node directories still on disk, and
nothing anywhere saying an operation was in flight. The only recovery artifact is an ad-hoc
`events.jsonl.bak-delN` copy that no reader knows about.

**Why it is not just fixed.** Adopting `ReceiptProtocol` means answering questions this review is
not the right place to answer:

* **Who owns the operation id?** The three siblings are all operator-initiated through HTTP and the
  operator supplies the id. This is initiated by an AGENT mid-run, which has no such id and no
  natural place to persist one across its own restart.
* **What is the phase lattice?** Reset's is an unordered adjacency table; deletion's is a monotonic
  index with an absorbing `quarantine_ambiguous`. A node purge is neither shape — it is closer to
  reset, but its "prepared" state spans two files that must publish together.
* **What does recovery DO?** For the operator-facing paths, an ambiguous outcome is quarantined and
  the operator is told to verify. There is no operator in this loop. Resuming automatically would
  have the engine re-enter a destructive rewrite of its own event log on a resume, which is a
  strictly worse failure than the one it fixes.
* **Should an agent be able to do this at all?** The honest alternative is to remove the tool and
  make node purge operator-only, at which point it inherits the deletion transaction wholesale.

**Recommendation.** Make it operator-initiated and route it through the existing deletion
transaction, rather than growing a fourth receipt protocol. That is a product decision about what
the agent is allowed to do to the run's own history.

---

## D-02 · The trace exporter's per-span filesystem cost · `core/tracing.py`

> **Status update (2026-08-14).** Unchanged.
> `core/tracing.py::JsonlSpanExporter._export_line_guarded` still runs the full ladder per row
> (hardened open, torn-tail heal, before/after identity stats, one append receipt per span), and
> `AsyncJsonlSpanExporter` still drains one row per delegate call — nothing is amortized per batch.
> Note the pointer comment this document promises "at its site" is NOT present in `core/tracing.py`
> (D-01/D-03/D-04 all carry theirs). How to close at the root: give the async worker a drain loop
> that takes the export lock + writer guard once per queue drain, heals once, appends the N prepared
> rows, and writes ONE receipt binding the whole appended byte range — the identity CAS then holds
> at flush granularity, which is the amortization choice the paragraph below already names. Measure
> before/after on the geesefs mount that motivated the numbers here.

**What it is.** Each exported span performs roughly three hardened opens (~12 `stat` calls), two
`flock`s, a torn-tail heal read and a 4 KiB read-and-parse of the append receipt journal, where the
previous synchronous exporter did one `open(path, "ab")` and one `write`.

**Why it matters.** On the node this codebase measures elsewhere — 14,507 spans — that is ~43k opens
and ~174k stat calls for a single node. On the geesefs/S3 mount a run root usually lives on, a
present-file `lstat` costs ~0.4 ms, so the metadata I/O alone is on the order of a minute per node,
paid on the single exporter worker thread whose queue is bounded and drop-newest. Overflow is
silently lost spans.

**Why it is not just fixed.** Every one of those probes was added deliberately, and the review
CONFIRMED the properties they buy: `O_NOFOLLOW` closes a regular-to-symlink race on a
service-written file, `O_NONBLOCK` stops a regular-to-FIFO race from consuming a worker forever, the
identity CAS catches a run-root replacement mid-write, and the receipt journal is what makes a torn
append detectable rather than silently truncating. Making this cheap means choosing which of those
to amortize — e.g. verifying identity once per batch rather than per span, or holding the descriptor
open across appends — and each choice trades a real guarantee for throughput. That is a security/
performance decision with a measurable downside on both sides.

**Recommendation.** Amortize per BATCH rather than per span: the exporter already has a queue, so
the identity ladder can run once per drain instead of once per row, keeping the guarantee at the
granularity of a flush rather than a span. Needs a measurement before and after, on the mount that
motivated it.

---

## D-03 · `card_trace_view` scans the whole run's spans · `serve/appstate.py`

> **Status update (2026-08-14).** Unchanged. The scan is still whole-run — `serve/appstate.py:613`
> carries the "KNOWN COST … docs/34 (CARD-TRACE-SCAN)" comment feeding `project_card_trace` — and
> `events/span_index.py` has no `card_id` or span-name dimension (`_SCHEMA` is 12; `card_id` appears
> nowhere in the module). The index-schema decision below is still the open one.

**What it is.** The card trace copies the entire run's light span list (a 1 GB run's index is ~220 MB
of Python dicts) and `project_card_trace` then rescans it once per owned node — ~1M predicate
evaluations for a card owning 5 nodes on a 200k-span run, on the request thread.

**Why it is not just fixed.** It cannot be handed only the owned traces. Research is matched TWO
ways, and the first is "a `propose` span carrying this `card_id`", which may live in a trace this
card does not own — that is exactly how the draft/debug/improve paths are reachable. A trace-scoped
selection would silently drop the research section for those, which is worse than being slow.

**Recommendation.** Add a `card_id` (or span-name) dimension to `SpanIndex` so the two match rules
can both be served by lookup. That is an index-schema change: it needs a `_SCHEMA` bump, a rebuild
path, and a decision about what else deserves a dimension before the row grows again.

---

## D-04 · `SpanIndex` hashes every source row · `events/span_index.py`

> **Status update (2026-08-14).** Unchanged. `_read_full` still SHA-256s each selected row's FULL
> bytes and `_scan_light` still retains a 64-char digest per row; `_SCHEMA` is still 12. The pointer
> comment is in place (`events/span_index.py:694`) and states the same bounded-prefix-plus-length
> trade this entry does. The corruption-class decision has not been made; when it is, make it as a
> `_SCHEMA` bump with the chosen preimage rule written at `_read_full`, so a weaker digest can never
> be mistaken for the full-bytes one by an older index.

**What it is.** `_scan_light` SHA-256s every admitted row and retains a 64-char digest per row in
memory and in the persisted index; `_read_full` re-hashes each selected row's FULL bytes on the
per-request conversation path, where a single generation span can carry 100 KB+.

**Why it is not just fixed.** The digests are what make the accelerator's central promise
enforceable — "returns None or less, never WRONG data". They are how `_read_full` proves a source row
still is what the index says it is, and how the node-window revision detects a rewrite that kept the
same size and mtime. Dropping them buys throughput by removing the verification, which is the wrong
trade for a component whose whole justification is that it may be trusted.

**Recommendation.** Keep the verification; reduce what it runs over. Hashing a bounded PREFIX plus
the length is strictly weaker but may be strong enough for the row-identity question specifically —
that is a decision about what class of corruption the index must catch, and it should be made
explicitly rather than by deleting a hash.

---

## D-05 · The catalogue tripwire keeps drifting · `ui/test/settingsSchemaResource.test.js`

> **CLOSED (2026-08-19).** The drift is bound, by a THIRD mechanism neither option in this decision
> proposed, and the status update below was **already false when it was written**:
> `tests/test_settings_ui_schema.py:161-179` reads `ui/test/settingsSchemaResource.test.js` as TEXT,
> extracts the pinned literal by regex and asserts it equals the derived
> `SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT` — so the Python suite a contributor actually runs fails
> on JS drift, with no CI-ordering assumption at all. It landed in `9644724e` at **2026-08-14
> 06:32 UTC**; the status update below landed in `1fa4ac9b` at **11:02 UTC the same day**, four and a
> half hours later, on a tree that already contained the guard (`git merge-base --is-ancestor` says
> yes). Its remark that the last two drifts were "both caught by the Python half first" is that guard
> working, read as luck. All four numbers agree today at **181** (`serve/settings_ui_schema.json`
> computed, `serve/settings_ui_schema.py:28`, `tests/test_settings_ui_schema.py:103`,
> `ui/test/settingsSchemaResource.test.js:90`), CI runs both suites on every push
> (`.github/workflows/tests.yml:15` and `:53`/`:65`), and the second option is deliberately NOT taken:
> the fixture already reads the packaged JSON, and the literal sits beside it on purpose so a change
> in the catalogue's SIZE has to be re-pinned deliberately — this decision's own text concedes that
> deriving it "would remove the tripwire's whole purpose". No marker: closing is a deletion, and what
> is left below is the record of what was decided.
>
> **Status update (2026-08-14).** Half-taken, and the half taken has not stopped the drift. CI runs
> the UI suite beside the Python one on every push/PR (`.github/workflows/tests.yml`: the `pytest`
> job plus a `ui` job running `npm test`), yet the JS literal is still hand-pinned and its own
> ledger now records a FIFTH (168→175) and a SIXTH (175→176) drift, both dated 2026-08-14, both
> caught by the Python half first — the contributor-side mechanism named below is intact, because
> `python -m pytest` is what gets run locally. The remaining root fix is the second option: derive
> the JS literal from the packaged `settings_ui_schema.json` at test time (or pin ONE shared count
> both suites read), so there is only one number to move.

**What it is.** The settings-catalogue field count is pinned in two files. It has drifted FOUR times
(162→163, 165→167, 167→168, and once more mid-review), always in the same direction: the Python half
moves and the JS half does not.

**Why it is not just fixed.** The ledger comment in that test now names the mechanism — the Python
guard runs in `python -m pytest`, which a contributor invokes, and the JS one runs in
`npm --prefix ui test`, which they may not. A fifth note will not change that. The fix is to make the
UI suite run where the Python one does, which is a CI/workflow decision.

**Recommendation.** Either run `npm --prefix ui test` in the same CI job as `pytest`, or derive the
JS literal from the packaged JSON at test time so there is only one number. The second is cheaper
and removes the tripwire's whole purpose — deliberate re-pinning — so it is a real choice.

---

## Not deferred, for the record

These were raised and are NOT open questions; they were refuted with evidence and must not be
"fixed" by a later reader who finds the original claim persuasive:

* the speculation lane's `attach` refusal does **not** double-pay — the branch is reachable only
  from the mechanical debug path, which makes no Researcher call (measured `propose_calls=0`);
* `systemic_failure_stop` **should not** drain forced requests — `leakage` and `aborted` do not
  either, and `tests/test_orchestrator_internals.py` pins that a hard terminal diagnosis and an
  extendable ceiling are different ladders;
* the oversized-row stop in `iter_bounded_trace_jsonl_lines` is the **specified** append-log prefix
  rule, stated identically at four layers, not a silent truncation;
* reordering `_ensure_process`'s pid publication would make a fork child **worse**, not better —
  the function holds no lock, so both threads would then race `_reset_process_state`.
