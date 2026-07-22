# Concepts

This explains the machinery behind a run. It's the *how it works* companion to the task-oriented
guides. For the full design rationale and decision records, see [`../00-INDEX.md`](../00-INDEX.md).

## The loop

A run is an `Engine` (orchestrator) driving four roles in a cycle:

1. **Researcher** — proposes an `Idea` (an operator + params), reasoning about the goal and prior
   results. Foresight ranks the candidates *before* an eval; the idea also states a one-line
   **hypothesis** that lands on the board.
2. **Novelty gate** *(opt-in — `novelty_gate`, off by default)* — an algorithmic reject of a
   proposal too close to one already tried (idea-text cosine ≥ `0.92`, or numeric param distance
   < `0.05`), with one informed re-propose. **Off by default** (and *not* turned on by
   `profile=thorough`): duplicate-detection is the agentic Researcher's call — it reads past
   experiments and *decides* — and the semantic/embedding search only **suggests** candidates for the
   LLM to adjudicate.
3. **Developer** — implements the idea as runnable code (or applies params to an existing repo). On
   a fresh repo node it runs three phases — **stages → plan → implement** (see
   [below](#the-developers-three-phases-stages-plan-implement)); a repair is one focused session.
4. **Sandbox** — runs the candidate in isolation with a timeout and output caps.
5. **Evaluator** — scores it (cross-validation, a held-out grader, or a repo's own eval command);
   then the trust gates + optional multi-seed confirmation decide what may be selected best.

The **policy** then picks the next node to expand, and the cycle repeats until a budget is hit. At
the end, the top candidates can be **confirmed** under multiple seeds, and the best becomes the
**champion**.

```
Researcher → Novelty gate → Developer → Sandbox → Evaluator → trust/confirm → policy picks next → repeat → champion
```

## Event log = canonical replay state

`events.jsonl` is the append-only source of truth for the **replayable run state**: nodes, metrics,
controls, approvals, terminal scopes, and numeric LLM usage. The engine writes domain effects; the
server writes serialized control intents; and the durable accountant may append `llm_usage` from a
background callback. `EventStore` serializes these writers across processes.

Not every artifact is an event. Bounded diagnostic trace representations (`spans.jsonl`), Assistant/run chat, logs, and node
workspaces are independent sidecars. They are useful evidence, but replay does not reconstruct them
and their absence does not change the folded research state.

This buys two properties:

- **Reproducibility.** `looplab replay RUN_DIR` folds the log into the current state with a pure
  function (`replay.fold`) — no side effects, identical result every time.
- **Crash-resume.** `looplab resume RUN_DIR` replays every complete event, reconstructs the durable
  frontier, and continues pending work. The reader tolerates a torn final line. External operations
  have narrower guarantees: an effect not yet represented by an event/receipt can be lost, while
  explicitly begun reflection work is not blindly redispatched as another outer logical operation.

The SQLite read-model and HTML/JSON tree projections are rebuildable from events plus whatever trace
sidecar still exists. Trace spans themselves are recorded diagnostic telemetry, not regenerable from
the event log, and are never read by `replay.fold`.

### Run directory

```
runs/<name>/
├── events.jsonl          # append-only event log — replay authority for RunState
├── config.snapshot.json  # resolved settings at launch (secret-masked)
├── task.snapshot.json    # verbatim task copy → the run is self-describing
├── engine.lock           # live-engine fence (one reducer per run dir; control appends serialize separately)
├── .commands/            # durable command records plus execution/activity claims
├── .llm-usage-outbox/    # numeric same-ID usage awaiting/confirming its event append
├── nodes/node_<id>/      # per-node eval workdirs (also confirm/ and ablate/ scratch dirs)
├── tree.html             # static lineage view (regenerable)
├── trace.json            # derived event/trace projection for the UI
├── chat.jsonl            # run-scoped operator/boss transcript sidecar
├── spans.jsonl           # recorded diagnostic telemetry; never read by replay
└── spans.index.jsonl     # derived bounded/redacted span projection + offsets
                          #   (versioned, regenerable cache; never a raw trace surface)
```

The **light span index** (`spans.index.jsonl`) is a derived cache the UI's trace views read instead
of parsing the whole (up to multi-GB) `spans.jsonl` on every click: it holds the same versioned,
allowlisted, bounded/redacted span projection used by the HTTP API, plus the byte offset
of the full recorded span in `spans.jsonl`. The run-level timeline reads only this much smaller file; a
per-span/per-node detail view seeks straight to the needed byte range. It is maintained incrementally
and is *strictly an accelerator* — if it is missing, stale, or corrupt the views transparently rebuild
it from `spans.jsonl` (the sole source of truth), so the safe indexed and fallback projections agree.
The raw recorded span dictionary is never copied into the persisted index or returned by a trace route.

The agent tool-loop re-sends the whole growing conversation to the LLM every turn, which once made
each generation's recorded input a near-duplicate of the last. That input is now **delta-encoded** at
write time — a generation that only appended to the prior turn stores just the appended messages plus a
back-reference — so `spans.jsonl` itself is ~6× smaller before the index even applies; the trace views
reconstruct the stored canonicalized/redacted diagnostic representation on demand. It is not byte-exact
provider I/O, and older JSONL is not rewritten by this encoding.

All browser-facing trace routes apply another explicit response budget (span/detail/conversation caps) and
return route-specific `projection` metadata. Individual spans carry `_projection` counters when fields, text,
messages, tool calls, events or nested items were omitted. A run tree, an operation tree, a single-span seek
and a bounded live tail expose different count fields; clients must not assume a uniform `total_spans` field.
Unknown fields never cross the allowlist; secret-named values are masked and secret-shaped identities are
quarantined. A failed read is marked `unavailable` without invented zero counts. The Inspector distinguishes
that state from a partial projection and an honestly empty trace. For collection routes, an absent span
sidecar is the latter (known zero observations); a request for one particular absent span is unavailable.

Because the task and config are snapshotted, a run can be resumed from its directory alone.

## Stopping & resuming a run — three verbs

The run-lifecycle subset of operator control has exactly **three verbs**:
`stop`/`finalize`/`resume`. Fork, reset, approve, budget, and other experiment controls remain
separate commands. Interactive Web, boss, and TUI paths first pass through the authoritative command
lifecycle below:

| Verb | Event | Effect | Wrap-up? |
|---|---|---|---|
| **stop** | `pause` | Freeze the run where it is. Reversible. | **No final wrap-up** — already-appended usage remains visible |
| **finalize** | `run_abort` → `run_finished` | Stop **and** run the end-of-run wrap-up | **Yes** — projections/tree, cross-run lessons + KB case, cost roll-up; an agent narrative report is generated only on configured natural completion |
| **resume** | `resume` | Continue from any stopped state (stopped / finalized / naturally finished) | — |

The one real difference is **finalization**. **stop** is a cheap freeze: it does not author the final
case/reflection/cost summary or terminal projections, although numeric usage already appended while
the run was live is not erased. An explicit finalize or a natural budget/search completion writes
`run_finished` and enters
the wrap-up. `resume` lifts an ordinary pause or a fully completed terminal state; it does not cancel
an incomplete terminal scope. The fold still understands legacy `run_reopened` as a resume alias.

### Authoritative command lifecycle

The Web UI, boss tools, and TUI submit interactive controls through one server-owned lifecycle rather
than treating an event append or process spawn as completion:

```text
GET  /api/runs/{run_id}/state
# copy the returned 64-hex generation fence

POST /api/runs/{run_id}/commands
Idempotency-Key: <one key for this logical action>
{"type": "resume", "data": {}, "expected_generation": "<state.generation>"}

GET  /api/runs/{run_id}/commands/{command_id}
POST /api/runs/{run_id}/commands/{command_id}/retry
```

The POST returns a durable command record. `accepted` and `executing` mean the request is still
pending; only `succeeded` or `noop` mean the requested postcondition was observed. `failed`,
`rejected`, and `timed_out` are terminal and include a safe structured error with retry/remediation
guidance. Clients poll the GET route for a bounded time and keep saying **requested/pending** if the
server has not reached a terminal state.

An idempotency key is scoped to one payload. A retry with the same key and payload returns the same
command, so a lost HTTP response cannot append the control event or start the engine twice; the same
key with a different payload is rejected. A failed/timed-out command whose intent is already durable
is retried through its same command ID, without appending the marked event again. A later GET can also
reconcile the record if the requested postcondition arrived after the original observation deadline.
The persisted record stores the key's digest, not the key.

The command's central `ControlSpec` decides whether it is fold-only, must ensure an engine is running,
or must preserve a pending stop while a driver finishes wrap-up. The service then observes the
matching postcondition — for example, **stop** requires paused state with no live driver and
**finalize** requires a non-error finished state with no live driver. Other engine-driving controls
complete when the engine emits an exact `command_ack` for that command ID and event sequence. That
ack means **the engine observed the intent**; it does not mean the downstream fork/reset/evaluation or
research work itself has finished.

The command service and engine acknowledgement monitor read the run log **incrementally**: an indexed
observation (`serve/command_observation.py`) scans each recoverable event byte once and retains a
bounded set of active-run indexes, so a long log's command volume no longer forces a full re-scan on
each observation pass. The engine's own ack cursor is the same shape — it inspects only the appended
suffix after bootstrapping the historical acknowledgement set.

Each control type also has an explicit payload allowlist. Unknown fields and lossy coercions are
rejected before append, so an ignored key cannot be persisted while the command reports success.

Decision, event append, and driver start are serialized per run. A pre-`Popen` lease covers the gap
before `engine.lock` appears. If a detached child remains cold past the observation deadline, the
lease is quarantined until its lock appears or its PID is definitively dead; timeout alone cannot
authorize a second `Popen`. A different active command returns structured
`409 command_in_progress`; an unresolved identical intent returns `409 retry_existing_command`, which
lets a reloaded client reattach without confusing one action's result with another. Command reads and
writes are `no-store`, so an intermediary cannot pin an old `accepted` response.

If a server crash leaves a quarantined spawn claim without knowable ownership, an operator can call
`POST /api/start/{run_id}/resolve-claim` after the recovery delay with the exact verification phrase
`I verified no LoopLab engine process is running`. The route cannot override a claim whose PID and
creation identity still match a live process. Worker execution claims use the same creation-identity
principle: elapsed heartbeat time alone cannot replace a possibly suspended live owner. New worker
claims publish a complete owner record with an exclusive hard-link rather than exposing an empty
authoritative file between create and write. Windows reads the native process creation FILETIME even
without optional `psutil`. For a pre-upgrade, malformed, inaccessible-owner, or filesystem-fallback
execution/activity claim, `POST /api/runs/{run_id}/resolve-activity-claims` is the explicit recovery
seam after process inspection and a safety delay; it requires the exact phrase
`I verified no LoopLab command or run activity is active` and cannot clear a claim whose exact owner
process generation is provably alive.

Creation identities are source-tagged (`psutil`, Windows FILETIME, or `/proc` start time). Unequal
tokens prove PID reuse only when both use the same source scheme; a cross-scheme or tagged/legacy
mismatch is inconclusive and blocks automatic takeover. This avoids replacing a live process merely
because the optional inspection backend changed between writes; delayed exact-phrase operator
recovery remains available when ownership is genuinely unknowable.

Each new terminal attempt opens a durable scope with its exact terminal payload before `run_finished`
and publishes `finalize_step:complete` only after the read-model build attempt (success or an explicit
best-effort skip) and successful trace/tree projections. Until that last marker, the canonical phase
is `finalizing` even if the engine died before or after `run_finished`; run list,
workspace, reset/delete, and legacy mutation guards all preserve the same recovery state. The
stop-aware `/resume` driver may finish it without appending a resume event. Wrap-up steps carry stable
scope gates, so a projection retry does not duplicate budget/diversity/cost events or already-marked
case/reflection work.
The effective latest terminal controls recovery: a later outer `run_finished(reason=error)` after a
scoped success/projection failure causes the original begun payload to be republished in that same
scope. A configured natural-finish report uses its own durable sequence inside the scope:
`finalize_step(begun, finish_report_planned=true) -> finalize_step(report_begun) ->` scoped
`report_generated -> finalize_step(report, outcome=...)`. Recovery can make the first
call when no attempt marker exists, reuses a durable report, and deliberately does not replay an
ambiguous begun attempt with no report. That last state is recorded as incomplete rather than risking
a second outer logical paid operation.
Reflection writes its begun marker before external/LLM work. If that attempt is interrupted, recovery
records it as incomplete without replaying the outer logical work; this avoids intentional duplicate memory
or model dispatch. Provider-client transport retries can still make billing ambiguous, so this is not an
at-most-once invoice guarantee.

Observed, run-attributable LLM usage is recorded as calls return, including calls made during
wrap-up, before finalization can complete. Each returned provider result produces a sanitized
numeric `llm_usage` delta (cost, call count, token counts, and an opaque usage ID only—never prompts,
responses, model URLs, or credentials). Same-ID retries are first-write-wins, covering an append that
committed and then raised. Engine roles and run-scoped boss/chat/per-run-report clients feed the run
ledger. Before the event append, the ledger first attempts to atomically persist the exact sanitized
delta as `.llm-usage-outbox/<usage_id>.json`; a successful outbox rename or event append is the first
durable boundary. The next reconciliation/metered/destructive boundary in a fresh process drains a
persisted record with the same ID; an exact stale record
is acknowledged after its matching event, while malformed, conflicting, symlinked, non-regular, or
unacknowledgeable evidence fails closed. A server client whose append remains unavailable is retained with its run-generation activity
lease. The next metered-route entry retries the same ID before constructing a provider client; a
reset/delete guard performs the same non-paid flush before taking its destructive sequencer. No new
provider request or destructive mutation starts while the usage remains pending. Reset/delete also
perform a store-only second drain under the destructive sequence; reset archives the outbox with the
old event generation, so generation-A usage cannot appear in generation B.
Non-run-scoped spend is excluded: global Assistant, Genesis/new-run planning, `/api/research`,
cross-run scope reports, `/api/llm/health`, and analogous CLI/global calls have no unambiguous run
attribution.

Replay uses the latest pre-ledger `llm_cost` summary as a compatibility base, then adds unique durable
deltas and ignores derived summaries as accounting input. A restart preserves deltas already appended.
There is still a measurement gap if the OS/server process dies after the provider returns but before
either the atomic outbox rename or event append completes. An ambiguous
timeout/reset/decode/empty-response retry can issue a fresh provider request after an earlier attempt
was accepted or billed but its usage was lost; without provider-side idempotency, only returned usage
is ledgered, so this total is not invoice reconciliation. A cancelled stream or any response missing
terminal usage records only what became known and cannot invent exact tokens or cost.
Finalization refuses to mark its cost step complete while a known in-process or outbox delta remains pending and
emits the presentation `llm_cost` summary after reflection. A custom `CostAccountant(limit=...)` is
not seeded from the durable total after restart and is not a shared multi-role budget guard; LoopLab
currently exposes no configured run-dollar limit, so this is a future enforcement boundary rather
than part of the displayed durable total.
The outbox makes known-delta restart recovery independent of the process-local retained-client
registry. LoopLab's command/activity service is nevertheless validated for its supported single UI
server process, not as a general multi-worker deployment.

The Web Dock and Assistant store a strictly allowlisted, sanitized envelope and acquire the same exact
per-run lock in tab-scoped session storage before POST. The lock binds the source, action, idempotency
key, and learned command ID. If storage is blocked or the write fails, the UI does not send the POST.
Malformed, tampered, mismatched, or unsafe stored state is quarantined as a non-resubmittable protocol
failure instead of being replayed.

Reload or a lost/network/rate-limited/invalid response shows explicit **status unavailable** recovery
with **Check same command**, not a new intent. With a known durable ID, Dock and Assistant GET/retry
that ID. If a POST response is lost before its ID arrives, they resubmit the same idempotency key and
deterministic payload, which resolves to the same server record. Assistant also restores a terminal
failure after reload and exposes **Retry same command** only when the record allows it. The shared tab lock makes a pending Assistant action visible to Dock
and blocks a competing same-run intent on either surface, while other runs stay independent. A direct
Assistant result remains attached to its originating run rather than appearing in a newly selected
run. Boss tools block a conflicting control while one command is pending. The TUI derives the command
ID from its key and durably stages that ID, exact key, and deep-copied intent **before** POST;
ambiguous 408/425/429/5xx or lost responses keep the row pending. An early 404 on an unconfirmed POST
causes bounded same-key/same-payload replay, so a delayed original arrival cannot turn a later fresh
click into an additive duplicate. The TUI stops an ordered plan at the first pending step.

Assistant command-backed run tools also have a durable per-session/turn mutation journal. A fresh
turn stages each normalized run-command intent before its side effect. Recovery may consume only the exact ordered
command-backed entries and reuses their keys; different/new intents are blocked, while direct storage
mutations are conservatively marked outcome-uncertain and not replayed. The recovered model receives
only read tools, Todo, and the journal-backed run-control provider: file/shell/git, knowledge writes,
MCP, run proposals, and subagents are absent. Its persisted model-facing instruction and permission
mode are pinned exactly; a changed raw instruction or mode is rejected with `409`. A different user
message is rejected while an unanswered turn is dangling. Cancel keeps the session's single-turn
slot until the old worker actually exits, so a new turn cannot overtake an already-issued mutation.
The Web client uses the same identity boundary after reload or server-process loss: it rechecks the
last durable `turn_id`, sends its persisted raw/display/mode once, and reattaches to the transcript
without creating a second logical turn. Identity corruption/mismatch is a blocked alert, not a
clean-content retry under the current mode. A completed persisted turn can be retried as a new turn,
but that retry still carries its durable raw instruction, clean display, and original mode.

Reset and delete coordinate with the run sequencer and refuse active command, execution, finalization,
or run-generation activity claims. Run-scoped LLM/report/chat work holds such an activity lease, so a
reset cannot redirect an old callback into the new event log. Terminal `.commands` records survive an
in-place reset, preserving accepted same-key idempotency across generations. State/SSE also exposes a
stable generation token. Web, Assistant, and TUI bind a fresh command to the generation they observed
before durable staging; the server checks it under the per-run sequencer before creating a record,
appending an event, or starting work. A valid delayed A request first arriving in B receives
`409 run_generation_changed` with zero effect, while a same-key A recovery still observes its old
record and never reapplies it.

Assistant tool approval is a separate server-owned safety boundary. A central action registry
classifies each concrete tool call as `READ`, `REVERSIBLE`, `CONSEQUENTIAL`, `HIGH`, or `UNKNOWN`;
missing/unregistered identities fail closed as `UNKNOWN`. Plan denies all mutation and does not expose
the shared-knowledge `remember` tool. Default asks for every mutation, Accept edits applies only
reversible edits inline, and Auto applies reversible/consequential actions inline but still asks for
arbitrary shell/test execution, destructive actions, external MCP, and unknown capabilities. A
remembered approval is never a broad tool-kind bypass: it is bound to the exact session, mode,
current turn/cancel epoch, action identity, and canonical scope digest for at most ten minutes.
`HIGH` and `UNKNOWN` actions cannot be remembered. Cancel, turn release, and session deletion
invalidate the matching grants. The permission card displays the server-derived risk, scope,
consequence, mode, expiry, and exact grant duration; legacy/incomplete metadata remains approvable
once or rejectable but cannot expose persistent approval.

The older `POST .../control` and `POST .../resume` routes remain compatibility surfaces. Legacy
mutation events cannot overtake an active/retryable command or incomplete finalize; the mutation-free,
stop-aware `/resume` route remains available specifically to attach a recovery driver. Current Web,
boss, and TUI controls use the command lifecycle above. Report regeneration remains a background job,
but its run-generation lease and cost events share the same destructive boundary.
Standalone legacy CLI `stop`, `finalize`, `resume`, and `approve` commands are not yet participants in
the server sequencer and must not be run concurrently with an active server-owned command. Migrating
those direct CLI paths is an explicit compatibility boundary.

**Reopening a *finished* run starts a new search epoch.** The nodes you add with the extra budget are
a fresh candidate set, so reopening bumps `search_epoch` and re-opens the promotion gates: the
multi-seed **confirmation** pass runs again (already-confirmed nodes are reused for free, only the new
candidates spend seeds) and, under HITL, **approval** is requested again for the possibly-new champion.
Without this, a champion confirmed in the first epoch would lock selection and a strictly-better node
found after the reopen could never be confirmed or win. (Reopening a merely *stopped* run — one that
never finished — is the same epoch and leaves those gates untouched.)

The UI also shows a node the **instant** the engine starts building it: a transient `node_building`
marker (folded to a `building` slot, *not* the event-sourced node set, so it never affects node-id
allocation or resume) that streams the node's live agent-trace right away, then is superseded by
`node_created` when the node materializes — or dropped if the run ends first.

## Search policies

The policy decides which node to expand next. It's pluggable (`make_policy`) and swapping it changes
no other part of the loop:

| `policy` | Behavior |
|---|---|
| `greedy` (default) | Greedy tree search with a multi-parent merge — strong because the *operators* do the heavy lifting |
| `asha` | ASHA / successive-halving: wide cheap base, keep the top 1/η per rung (`asha_eta`, `asha_rung_nodes`) |
| `mcts` | Monte-Carlo tree search |
| `evolutionary` | Evolutionary policy with a diversity archive (`archive_resolution`) |

Add `policy=bohb` behavior by combining ASHA racing with the surrogate proposer
(`surrogate_proposer`).

## Operators

The win comes from rich operators, not exotic search. The Researcher/Developer apply:

- **draft** — a fresh candidate.
- **improve** — refine the current best.
- **debug / repair** — on a crash, hand the failing code + stderr back to fix it. Mechanical crashes
  can be repaired **in place** within the same eval (`inline_repair`), which doesn't consume the
  node budget; deeper failures get a structured "reproduce then fix" directive (`deep_repair`).
- **ablation-driven refinement** — neutralize a parameter (or a whole code block with
  `ablate_code_blocks`) to find the highest-impact lever, then refine it (`ablate_every`).
- **merge / ensemble** — recombine two parents: a param mean, or a code-recombination ensemble
  (`merge_mode=ensemble`). This is the multi-parent DAG.
- **sweep** — one node runs a whole grid of trials in a single process.

## The Developer's three phases (stages → plan → implement)

On a fresh (non-repair) implement of a repo node the Developer runs **three separately-traced
phases**, each its own focused tool-loop so the context stays small and the trace reads cleanly
(`Developer · stages → plan → implement`):

1. **STAGES** (mandatory, **first** — unless the operator pre-empts it, below) — a **read-only**
   phase whose only exit is a `declare_stages`
   emit. The repo-savvy Developer studies the repo *and* the operator's `cmd`, then declares the
   ordered eval pipeline (`data_prep → train → …`) that runs **before** the operator's protected
   `score` step, baking **this node's** hyperparameters into the `train` command. It writes
   `looplab_stages.json`. The Developer owns the stages, **not** the planner/Genesis — the phase is
   skipped only when the **operator** pre-empts it: a valid `cmd.stages` pipeline (the engine uses
   it verbatim; the Developer implements the code those stages run) or a protected
   `looplab_stages.json` (the knob that disables Developer pipelines — skipping avoids burning an
   LLM loop whose manifest would be dropped). Good practice: separate stages for data/feature
   **preparation**, **training** (a fresh model every node — never reuse a checkpoint), and
   **testing**.
2. **PLAN** — the read-only atomic-step decomposition (`propose_plan`; **C4**,
   `developer_plan_decompose`), unchanged.
3. **IMPLEMENT** — writes the code the stages run, one bounded session per plan step.

A **repair** (an error to fix) stays a single focused session — no stages, no plan.

The **cmd-context rule** governs the stages phase: the operator's `cmd` is passed in as context. If
it is **present**, it is shown as **immutable** — the Developer declares only the *preceding*
stages. If it is **absent**, the Developer must declare the **full** pipeline, including a final
stage that runs the evaluation and prints the metric. Either way the stage name `score` is
**reserved** (it always denotes the engine-appended operator step) — with no `cmd`, name the
scorer e.g. `evaluate`.

## Multi-stage eval pipeline

For a repo task the eval is a **declared pipeline of named stages** instead of one opaque command.
The Developer declares the **preceding** stages in its dedicated STAGES phase above (or the operator
sets them on the `cmd` via `eval.stages`); the operator's `cmd` is appended as the final, protected
`score` stage (the trust boundary — the agent adds work before scoring but never rewrites it):

```json
{"stages": [
  {"name": "data_prep", "command": ["python", "prep.py"]},
  {"name": "train",     "command": ["python", "train.py"], "timeout": 7200, "check": true}
]}
// + the operator's cmd (e.g. ["python", "test.py"]) runs last as the protected `score` stage
```

Stages run in order in the **same workdir** so artifacts persist (train writes a checkpoint → eval reads
it). Each stage gets its own span + `<name>.log` and a pass/fail (`stage_finished` events fold onto
`node.stages`). Three payoffs:

- **A crash is pinpointed** to its stage (`node.failed_stage`), not hidden behind one command — a run
  that never actually trains is obvious (no `train` stage / a red one).
- **Fix only the broken stage** — re-run the node *from* a stage (the Overview's clickable "eval
  pipeline" strip, `reset(stage)` in chat, or a `node_reset` with the stage name): earlier stages are
  marked *reused* and skipped, so a failed `eval` is fixed in seconds without paying to re-`train`.
- **Optional inter-stage verify** — a stage flagged `"check": true` hands its output to an agentic
  checker (Researcher/Developer) before the next stage runs; a concern stops the pipeline early so a
  diverged train can't silently feed eval.

The operator's `cmd` is the **authoritative, non-rewritable scoring stage** and its stdout is where the
trusted metric reader reads. The Developer's STAGES phase supplies only the stages that run BEFORE it
(`data_prep`, `train`, …); the engine appends `cmd` as the final protected `score` stage. When `cmd`
itself declares `stages`, those are canonical (the agent implements the scripts, not the structure). With
no operator `cmd` at all, the STAGES phase declares the full pipeline including the final scoring stage.
A `%params%` token in any command expands to the node's tuned hyperparameters.

## Evaluation rigor

A reported number is only useful if it generalizes. The trust layer is leakage-first:

- **Consistent cross-validation** — K-fold and purged walk-forward (no look-ahead) so every
  candidate is scored the same way.
- **Leakage detectors** — train/test contamination, target leakage, and temporal leakage are
  flagged.
- **Variance gate** — a candidate must beat the incumbent by more than ~1 standard error to be
  promoted, so noise doesn't crown a lucky run.
- **Optional multi-seed confirmation** — when `confirm_top_k` and `confirm_seeds` enable it, re-run the
  frontier under several seeds and pick the robust best. It is off by default; without it the selected
  winner remains explicitly single-evaluation and seed luck has not been ruled out.

The replay model keeps these promotions honest across resets and reopens:

- **Attempt identity** — re-running a node in place (`node_reset`) bumps that node's *attempt*
  generation. A late terminal from the attempt the reset abandoned (its eval was still in flight)
  carries the old generation and is dropped, so it can't land a metric from the discarded code onto
  the new attempt.
- **Subject-bound approval** — a human `approve` grant is folded only if it names a real candidate
  node; a forged/stale grant for a node that isn't in the run can't silently flip the run to approved.

## Trust & the sandbox

The sandbox tier is chosen by **trust mode**, not your environment (`make_sandbox`):

| `trust_mode` | Sandbox | Use |
|---|---|---|
| `trusted_local` (default) | `SubprocessSandbox` | Your own research on your own box. Process isolation + timeout + tree-kill + output caps. **No Docker.** |
| `untrusted` | `DockerSandbox` (`--network none`) | Executing untrusted code on shared infra (hosted/multi-tenant UI) |
| `hostile` | `DockerSandbox` (`--network none` + gVisor `--runtime runsc`) | Actively hostile code — a real kernel-level isolation boundary |

Additional safety monitors are off by default. Under the default `trust_gate=audit` they only surface signals;
`gate`/`block` acts only on high-precision signals:

- `redact_output` — mask credentials in stdout/stderr before they're persisted.
- `reward_hack_detect` — flag suspicious wins (grader/answer-key access, frozen-file writes,
  suspiciously perfect metrics).
- `code_leakage_detect` — static scan for fit-before-split / fit-on-test.
- `critic_check` — an execution-free critic of each solution. Broad critic warnings stay advisory;
  `critic:hardcoded_metric` is the narrow high-precision exception that can gate.

Heuristic perfect-score, audit-unavailable and suspicious-output warnings remain advisory in every mode.
High-precision reward-hack/leakage signals (and `critic:hardcoded_metric`) exclude a node from best-selection
and breeding/confirmation under `gate`; `block` additionally makes it infeasible.

These surface in the UI's Trust panel as audit events. See [Deployment](deployment.md) for the
untrusted tier.

## Meta-control: the Strategist & unified agent

- **Strategist** (`strategist_backend`, default `agent`) — a meta-controller that adapts the
  search policy, operator mix, and fidelity per situation. It defaults to the **agentic** backend: a
  tool-using loop that *reads* the run / data / siblings / KB / memory before deciding (the `llm`
  backend is a single-shot call over aggregate stats; `rule` is a fixed heuristic; `off` runs fully
  static). Every choice it makes is also a direct config knob, so you can run fully static (`off`). At its consult cadence it reads a **coverage
  read-model** (`coverage_context`, on by default): a deterministic breadth summary of the run so
  far — the distinct **concept axes** occupied and parameter-niches, the axis entropy, and the
  dominant-axis fraction — recorded as a `coverage_snapshot` audit event (the run's *narrowing
  curve*). Breadth is read over the **folded per-node concept set** (multi-membership: a node counts
  under every axis it touches), so re-tags and consolidation renames reach the signal and it agrees
  with the /concepts map — no longer the Researcher's first-authored theme. Concentration divides the
  leading-axis count by the larger of idea count and total axis memberships: untagged ideas dilute it,
  while genuinely multi-axis work is not misreported as 100% concentrated on every shared axis. This is context, not
  a decision: it gives the controller eyes on whether the search is broadening or collapsing onto a
  single line of attack, so breadth can be a deliberate signal rather than only a reaction to metric
  stagnation. From that reading the Strategist sets a **novelty stance** (`explore` / `balanced` /
  `exploit`) — the single dial for how hard the run pushes for NEW directions. `balanced` is today's
  behavior; `explore` (chosen when coverage shows narrowing) threads one directive into the three
  places ideas are shaped — the Researcher's proposal (propose a different theme), the foresight
  rank (break near-ties toward the more divergent candidate), and the novelty gate (engage a soft
  dedup + one informed re-propose even when the static gate is off) — so novelty pressure is one
  meta-decision, applied coherently, and always via the LLM roles rather than a hard-coded rule.
- **Card-driven selection** (`card_driven_selection`, off by default) — lets the receipt-backed Card
  queue own the next macro action instead of the policy/pilot arm. The run-start record pins this
  choice, and Card authority wins if `agent_drives_actions` is also enabled. The Strategist can shape
  the separate atomic `card_scoring` treatment (explore/balanced/exploit plus bounded novelty and
  coverage weights); it ranks only Cards that have already passed durable readiness and live-anchor
  checks.
- **Unified agent** (`unified_agent`, on by default) — one LLM identity plays Researcher +
  Developer (+ Strategist) across stages, choosing its model/toolset per stage and driving the next
  macro action within a *pure legal-action gate* that keeps pipeline discipline. Set
  `unified_agent=false` and `agent_drives_actions=false` for the legacy split-role behavior.

What an agent may change at runtime is governed by `agent_control` (a per-setting allow-list of
roles) — see [Configuration → Strategist & meta-control](configuration.md#strategist-meta-control).

## The concept graph & concept views

Every experiment carries a **set** of research concepts — multi-label `axis/slug` path ids like
`loss/contrastive/dcl` — instead of a single free-text grouping slug. The Researcher **authors** that
membership on the `Idea` with an explicit contract:

- `concept_mode="full"` means `concepts` is the node's exact complete set. New writers emit this mode
  even when the set is empty. An old event with no discriminator keeps its historical full-set behavior;
  an old payload with no membership remains absent rather than becoming a known-empty classification.
- `concept_mode="delta"` means `concepts_added` / `concepts_removed` modify the inherited set. A root
  inherits `RunState.run_base_concepts` (from `run_concepts`); a child inherits the union of its parents'
  effective sets. Both lists may be empty: that is an explicit **zero delta** (inherit unchanged), not an
  absent membership. Bounded valid operands remain in `RunState.node_concept_deltas`; the append-only
  event is the raw audit source. Replay materializes the effective full set in `RunState.node_concepts`
  after the complete DAG has folded.

Replay normalizes ids (case, surrounding whitespace/slashes, spaces to hyphens) and resolves the bounded
`concept_consolidation` rename chain (at most 16 hops) on the base, inherited values, removals and additions
**before** set subtraction/union. Thus `Model/Transformer` can be removed by `model/transformer`, and
consolidation cannot resurrect a renamed id after subtraction. A classifier, operator or offline display
receipt remains
authoritative over an authored delta for the unchanged Idea; only classifier receipts count as independent
evidence. A `propose` reset clears authored membership, provenance and the bounded delta sidecar together. The short-lived
pre-discriminator format with a non-empty delta list remains readable and canonicalizes to
`concept_mode="delta"`. Modern producer schemas require the discriminator; tolerant replay preserves
genuine absence on historical events instead of serializing it as an invented full set.

Replay records bounded, canonical integrity envelopes in
`RunState.node_concept_materialization_receipts[node_id]` with `status` (`partial` or `unavailable`) and an
ordered closed list of `reasons`. Invalid ids, rename failures and the 64-concept cap preserve the valid
subset as partial. Unsupported modes, invalid consolidation maps, a delta root whose `run_concepts` base
event has not arrived, missing/unknown parent membership and active dependency cycles are unavailable; they
fail closed to an empty effective set and propagate to active descendants. An explicit `run_concepts` event
with `concepts=[]` is a known-empty base, distinct from an absent event; a late valid base clears the
unavailable prefix receipt. `RunState.run_base_concept_receipt` applies the same distinction to the run base and
disables new delta authoring unless inheritance is exact. `ConceptFrame` becomes incomplete and
non-authoritative whenever an active receipt exists, so the UI cannot mistake a fallback for an explicitly
authored empty set. Current frames ignore node receipts belonging only to tombstoned or aborted nodes;
historical prefixes retain them and remain non-authoritative.

`ConceptFrame.completeness.reasons` distinguishes safe, bounded cap receipts (for example
`membership_cap`) from corruption-class receipts such as `concept_mode_unsupported`,
`delta_dependency_cycle`, `delta_dependency_missing_run_base`, `delta_dependency_missing_parent`,
`delta_dependency_unknown_parent_membership`, `invalid_concept_materialization_receipt`,
`invalid_consolidation_map`, invalid membership input, `invalid_concept_id`, `rename_cycle`, and
`rename_hop_cap`. Cap receipts expose a deterministic safe subset; corruption receipts mean that missing
membership cannot be interpreted as absence. These are read receipts over durable run state, so refreshing
the same frame does not repair them. Inspect **Lab → Events** and **Authoring** to identify the broken
delta/consolidation source, then use a supported operator re-tag where appropriate or fork and replay a
corrected run. Preserve the event log as the audit source; do not hand-edit a derived projection/cache.

The per-node `node_concept_delta` read model and the Researcher-facing concept tools preserve the same
distinction. Exact results retain their original `{parent_ids, added, removed, inherited}` shape; an
incomplete result adds `partial=true` or `unavailable=true` plus ordered `reasons`. Unavailable membership
infers no delta. A partial child keeps only retained `added`/`inherited` lower bounds, suppresses
`removed`, and publishes `unknown_dimensions=["removed"]`: absence from a capped/invalid subset cannot
prove that an inherited concept was removed. Aggregate
tree/membership tools combine receipts only for current nodes, so they neither claim exact absence during
an unresolved materialization nor let a receipt belonging only to a tombstoned/aborted node poison the
live view. `list_themes` and `list_experiments(theme=...)` cross that same receipt/lifecycle boundary:
unavailable memberships never revive frozen authored concepts, retained partial or legacy matches are labelled
as hints rather than exact results, and an incomplete no-match is not reported as a complete zero. The unfiltered
`list_experiments(sort="recent")` also excludes tombstoned and aborted audit rows.

Full or materialized memberships fold into `RunState.node_concepts` at/after `node_created`
(deterministic, offline — no tagging cadence required), and the strategist cadence may later
consolidate/enrich them. An **operator** can also re-tag one node's concepts directly via the durable
command `concept_tag_edited` (generation-fenced like a comment): it folds with `operator-edited` provenance,
which the classifier re-tag cadence **must not clobber** (order-tolerantly, invariant 5). Generic node resets
do not clear the override. Only a `propose` reset clears it together with the Idea;
implement/eval resets preserve it while the Idea is unchanged. Operator edits are authoritative for the
run's read models but are deliberately **not**
promoted to independent classifier evidence. Membership is not a metric, independent evidence or a direct champion score.
The same provenance boundary applies to retro-tagging: `concept-coverage --offline --persist` records exact
`offline-heuristic` provenance, so its coarse alias matches appear in the UI but cannot feed graded-novelty
admission or cross-run capsules. Only genuine legacy classifier events (which predate the `mode` field) and
the exact reviewed `llm` / `agentic` producers fold as `classifier`; explicit malformed or future modes fold
as `untrusted-source` until reviewed. A later agentic pass upgrades identical heuristic ids exactly once,
while an offline/future event can never overwrite or downgrade existing classifier evidence.
Because retro-tagging runs after terminal finalization, classifier tags appended this way are available to
indexes rebuilt from event logs but do not retroactively rewrite the run's already-emitted cross-run concept
capsule. Capsule regeneration is a separate maintenance operation; the command never claims it happened.
When enabled, however, `concept_pivot` coverage and `graded_novelty` deliberately use the recorded concept
claims to steer exploration/proposal admission; disabling those controls restores the ordinary non-concept
search path. UI rollups remain descriptive and do not independently verify taxonomy semantics.

The old single-slot “Direction” is not a second semantic model. Current compatibility surfaces call it
**primary concept axis** and derive it from the folded state: canonicalize the node's current memberships
through consolidation aliases, take their distinct top-level axes and choose the lexicographically first.
An explicit empty `node_concepts` row is authoritative and stays untagged; it must not revive an old
`idea.theme`. Only a genuinely missing folded row may migrate through legacy `idea.theme`, then the first
authored concept axis. This projection is intentionally lossy and run-local. The retired run-URL `focus`
parameter is ignored with a visible notice directing the operator to the Concepts filter.
On a mixed-era run, that legacy fallback may still group the Search DAG while the Concepts view remains
honestly empty until folded memberships exist; the UI explains the distinction instead of treating authored
legacy text as canonical ConceptFrame membership.

**Hierarchy is a projection, not a stored tree.** Because concepts form a graph, "what is a top-level
axis" is a *perspective*. A **typed concept-edge log** (`EV_CONCEPT_EDGE` → `RunState.concept_edges`,
folded commutatively — max-confidence-wins per `(src, rel, dst)` triple) retains asserted relations such
as `is_a` cross-parents, `uses`, and `part_of`. `co_occurs` is deliberately **not** durable: it is derived
from the current bounded folded memberships on every ConceptFrame read. That lets its count decrease or
the pair disappear after re-tagging, and gives online, offline, and legacy runs the same projection; old
persisted `co_occurs` cache rows are ignored. A hierarchy is then **computed** by a pure read-model:

- `project_hierarchy(ids, lens="is_a")` — nests by the concept **path** (the default lens; an empty
  edge set falls back to this, byte-identically to the old axis-prefix tree).
- `project_lens(ids, edges, lens, touch=…)` — nests by a typed relation: directed `uses`/`part_of`,
  or symmetric `co_occurs` oriented by touch (most-used concept = hub). One primary parent per concept
  + `cross_parents` for the memberships it drops; deterministic, cycle-safe.
- `derive_lens(prompt, edges, client)` — an agent that **mints a lens in the moment** from a
  natural-language request. This low-level helper returns a pure projection spec and does not itself write
  events. The owner HTTP/UI path around it is a separate explicitly paid durable workflow described below;
  callers must not infer free or replay-clean transport from the helper alone.
- `concept_metrics(state, graph, tags)` — per-concept `{touched, evaluated, best, mean, delta_best,
  delta_mean, first_touch}`; a multi-membership node's metric counts **fully in every concept it
  carries** (never divided). Current rollups and the median baseline exclude tombstoned/aborted lifecycle
  rows; best/mean eligibility also requires an evaluated, finite metric that is not explicitly infeasible.
  `delta_*` is direction-normalized vs that median, so positive means better for both minimize and maximize
  runs. See `looplab/search/concept_graph.py`.
- `node_concept_delta(state, node_id)` — one node's concepts as a **delta vs its parent(s)**:
  `{parent_ids, added, removed, inherited}` (a merge inherits from the UNION of parents; a root's concepts
  are all `added` for legacy full authoring, while a delta-authored root inherits the run base). This is a
  pure projection over materialized full-set `node_concepts`, distinct from the optional bounded authored delta
  sidecar. Both sides canonicalize through the consolidation rename, so it shows what each experiment
  conceptually changed relative to where it came from. Missing parent references fail unavailable rather
  than being silently reinterpreted as a root. Surfaced to the Researcher/Strategist via the
  `node_concept_delta` tool.

These are surfaced at `GET /api/runs/{id}/concepts?lens=…` as a bounded `ConceptFrame` v1: one exact
run generation and captured event prefix, completeness/authority receipts, a per-lens tree, metrics and
self-contained experiment refs carrying node attempt/generation. Current projections consistently exclude
tombstoned nodes and IDs in `aborted_nodes`; those records remain in append-only audit history. Cap
truncation is labelled partial, and malformed/corruption-adjacent source reasons are a stronger unsafe state.

The frame drives two run-view surfaces:

- **Concepts** is a concept tree/table with concepts at arbitrary depth, experiments nested under the exact
  concept they touched, configurable metric columns and a **Projection lens** switcher. Edge-projection copy is dynamic:
  its heading/legend names the validated active `requested_lens_spec.rels` vocabulary, indentation is one
  primary **display** parent, and expandable `+N links` exposes exact additional projected parents. The
  `co_occurs` projection explicitly says that its links are derived from current frame memberships rather
  than recorded edge claims. Loading and
  recoverable-error states retain the selected projection vocabulary. Counts say **displayed concept nodes**;
  bulk controls say **Expand concept rows** / **Collapse concept rows**. The view always states
  objective scope, missing metric display name/unit, minimize/maximize orientation and normalized Δ
  semantics. Row order remains the hierarchy/relationship projection order; enabling a metric column does
  not silently sort by Δ.
- **Search** adds canonical breadcrumb chips over the lineage DAG. Chips are sorted by canonical ID so live
  count changes do not move controls, support minimal OR subtree selections, retain a trailing exact “· here”
  chip when a drilled path has direct memberships, and highlight current matching nodes. Expanded and
  collapsed groups use the same active-lifecycle boundary; an active filter shows matched/total, dims a zero
  match and computes best/status/trajectory only from matching eligible members.

Both surfaces carry quick-search. The chip search previews the graph highlight and pins a concept on
Enter/click; the Concepts header filters concepts and their experiment refs (node id/status) and auto-expands
paths to matches. Both operate client-side over already validated loaded state. Concept tagging ships **on by
default** (`concept_pivot`).

### Paid derived-lens lifecycle

Ordinary ConceptFrame GETs and shipped lens switches are read-only. **Create lens · paid** is the explicit
provider boundary. Before dispatch the current browser stores one run-, generation- and prompt-bound identity.
The server validates the exact generation and ConceptFrame input, durably appends
`concept_lens_started`, then runs one logical `tool_call_once` worker operation through the metered run client
and persists a completed/failed/declined terminal. The same identity rejoins or replays the existing logical
work; parser repair and outer same-identity redispatch are suppressed. The core client may nevertheless make
bounded transport retries, so one HTTP/provider attempt, one invoice charge and complete billing
reconciliation are not guaranteed. A bounded cap-only partial frame may be used and remains labelled partial,
while any completeness reason outside the explicit safe-cap allowlist blocks provider construction.

Reload recovery is owner-plane GET-only and intentionally returns no prompt, digest, paid idempotency key or
resolution key. It can poll the exact current job, restore a terminal, or report an orphan/conflict. Explicit
orphan resolution uses a separate resolution idempotency key plus the recovered request ID and started
sequence, sends no provider retry, and leaves provider completion/billing unknown. Review links, historical
snapshots, unavailable recovery storage, ledger conflicts and pending cost accounting all fail closed for new
paid identities.

## Cross-run memory

Cross-run memory is **on by default** — `memory_dir` / `knowledge_dir` default to
`~/.looplab/memory` and `~/.looplab/knowledge` (set `LOOPLAB_MEMORY_DIR=""` to disable). The best
result of each run is retained as a **case** (retain-on-improvement); at run end `reflection_priors`
(also on by default) distills a causal **meta-note** ("why the winner won"), generalizable
**lessons** (good *and* bad, with a verdict + evidence count), and reusable **skills** — all stamped
with a task fingerprint and matched into the next similar run's proposal prompt. Duplicate lessons
are merged (exact-hash **plus** a hybrid-retrieval → agentic paraphrase-merge pass); the in-run
**hypothesis board** is deduped the same way and prioritized by foresight. See
**[Memory & knowledge](memory.md)** for the full tier-by-tier breakdown.

This shipped lesson/case memory should not be confused with the complete **portfolio research index**.
An experimental Part-IV slice now ships enabled by default in product `Settings` (the bare-library
`EngineOptions` defaults remain off): rebuildable run passports/facts, per-run concept capsules,
versioned concept-key alias/split overlays, v3 D8 claims, task facets, bounded retrieval, and backend
Atlas/claims projections. Its bound agent tools use role and compatible direction; lessons/capsules accept
exact task or a strict related-goal fingerprint, while v3 D8 is exact-task-only because it stores no goal
fingerprint. Each explicitly processed v3 D8 run carries a producer total/retained/omitted receipt, and
readers carry independent lesson/research JSONL read-health; malformed, schema-invalid and unknown-future
rows are quarantined rather than interpreted as evidence. A processed-empty run therefore leaves a durable
zero/zero sentinel, but these receipts do **not** prove that D8 ran for every historical portfolio run.
Legacy v0-v2 rows remain readable, but their producer denominator is unknown.
Any incomplete producer or read-health receipt makes retained claim counts and absence lower bounds and
withholds exact one-sided states across Atlas, retrieval, tools and advisory prompts. Task facets are
advisory metadata reserved for future ranking and currently neither grant
visibility nor change ordering. External coding-agent Developer backends receive no D8 provider. Proactive
prompt influence carries lean digest receipts. The `cross_run_concept_map` tool computes exact node/run totals
from the validated retained capsule snapshot, but deliberately limits edge generation to the top 512 graph
nodes before pair materialization. Its edge receipt distinguishes response-capped edges known within that
projection from edges touching pruned nodes, whose count remains explicitly **unknown** rather than reported
as zero; capsule-source completeness is a separate receipt. Typed
owner governance actions add revision/action fencing and explicit clear operations — reachable both from
the `/api/cross-run/concept-*` endpoints and, for the owner assistant, from mode+approver-gated
`concept_merge` / `concept_purge` / `concept_split` / `concept_edit_clear` tools (read-only
`concept_taxonomy` is available even in plan mode). Assistant taxonomy reads are a redacted,
`UNTRUSTED_MEMORY`-framed projection capped at 16K; they include active split rule semantics and exact
alias/split/global revision receipts. Every assistant edit binds its approval to the normalized payload and
those revisions, so a concurrent HTTP/CLI edit rejects the stale action. Clearing a purge (un-purging) is a
high-risk transition and therefore still asks in Auto, while clearing an ordinary merge/split remains the
normal consequential edit. The remaining heuristic
scope, incomplete comparison/access/health receipts, missing evidence/taxonomy releases, and attempt-level rather than independent evidence
families mean it is not yet the production 50–500-run system. The wired owner `#/atlas` route is explicitly a
bounded read-only preview of these projections, not that full system.
The advisory `concept_card` lookup reuses exact/fuzzy slugs, keeps scoped track record separate from the global
observation count and frames persisted text as untrusted. It is not a prose-paper generator, verifier,
governance mutation or complete applicability receipt.
The target CR0–CR3 design adds the full applicability/coverage frame, durable derivation contracts,
incremental summaries and interactive Research Atlas; see
[Project review §21.20](../17-project-review-and-directions-2026-07-11.md#cross-run-research-architecture).

The per-run `cross_run_prior` timeline signal remains an audit-only preview. Its v2 receipt separates a
prior run's `run_best_metric` from `matched_concept_outcomes`, but the current timeline deliberately
renders neither metric as evidence authority: it shows only bounded matched concepts, a retained-run
count, and **evidence completeness unknown**. Inspect the event receipt or cross-run tools for the
run-best/outcome distinction; capsule or bounded-run omissions are never inferred as complete history.

### Harmonic memory (`memora`, optional)

An idea import from [Memora](https://github.com/microsoft/Memora) (Microsoft Research, ICML'26).
**On by default** (`memora=true`): the case library + knowledge index key each memory not by its
**raw text** but by a short **abstraction** (a 6–8 word essence) plus a few **cue anchors** (tags
giving alternative retrieval paths). Three things follow:

1. **Abstraction + anchors as the index** — only the abstraction/anchors are embedded; the rich memory
   value is stored alongside, unindexed.
2. **Consolidation on write** — a new memory whose abstraction closely matches an existing one is
   *merged* into it (union of anchors, better metric kept) instead of adding a near-duplicate, so the
   index carries roughly half the entries of a flat store.
3. **Anchor-expansion on retrieval** — `kb_search` / case lookup follow the top hits' anchors to
   surface *related-but-not-similar* memories the plain query missed.

**LLM-optional by design.** Abstractions are written by the wired chat model (`memora_llm=true`, the
default) — **cached** by content hash so a re-built index never re-calls the model on unchanged
notes/cases, and degrading to a deterministic **lexical** abstractor whenever the endpoint is
unreachable (so an offline box just gets lexical abstractions, never a crash). Set `memora_llm=false`
to force lexical everywhere, or `memora=false` to restore the pre-Memora raw-text index. Like the rest
of cross-run memory, abstractions live only in the derived, rebuildable retrieval index — never in the
event log or the canonical `cases.jsonl`.

## Observability

Every step emits a trace **span** to `spans.jsonl` (files-as-truth, zero-dep). With `trace_llm_io`
on (default), each LLM call records a bounded, canonicalized, heuristically redacted diagnostic
representation of its input/output. The provider still receives the original input; trace capture is not
byte-exact, short unlabeled secrets can evade heuristics, and existing JSONL is not retroactively rewritten.
Installing the `[otel]` extra and setting `OTEL_*` sends the same
spans to any OTLP collector (Jaeger / Tempo / Honeycomb) with no code change. Spans are diagnostics
only — `replay` never reads them.

**Per-operation traces.** A node's own work (propose → implement → repair, then evaluate/training)
is one trace, shown under the node. But every OTHER LLM sub-operation runs in its **own** named trace
(`new_trace`) — `strategist_consult`, `hypothesis_merge`, `deep_research`, `report`, `lessons_distill`/
`lessons_refresh`, and the two Researcher ranking steps — `hyp_prioritize` behind `hypothesis_ranked`
(board prioritization) and `foresight_rank` behind `foresight_selected` (idea predict-before-execute). The event
that operation emits is **stamped with that trace's id** (the event store reads the active span's ids
on append; a telemetry event whose op-span already closed carries the captured id explicitly), so the
UI expands that event's row to ONLY that operation's trace — never the whole node's Researcher+Developer
tree. `GET /api/runs/{id}/trace/by_trace/{trace_id}` returns one operation's span sub-tree; the node's
full trace is at `/api/runs/{id}/trace`. Events with no LLM (e.g. `coverage_snapshot`, deterministic)
carry no trace; a `node_evaluated` row shows the **training** run (the `evaluate` span), not an LLM call.

Every one of these auxiliary LLM steps is now **agentic** (via the shared `agentic_text` /
`agentic_struct` helpers): lessons distillation (reflect / comparative / skill / causal), the
research + reward-hack / leakage **verify** pass, the end-of-run **report**, and **Genesis** (goal →
task plan) each *read* the real experiments / code / data through read-only tools before emitting,
rather than reasoning single-shot over a text preview. (`best_of_n` and `hybrid_merge` ride the same
agentic path but with `tools=None` — there is no run state to read at those call sites.)

**Live status.** The UI reads what an LLM is doing right now from the append-only markers: a
`node_building` marker (emitted the instant `_create_node` starts, before the minutes-long author step)
drives a `✍️ writing` / `🔧 repairing` / `🔀 merging` status (by the node's operator) and streams that
node's trace live; a `pending` node is being **trained** (the sandbox eval — no LLM), shown as
`running (training)` with no live pulse. The assistant chat streams the same way — interstitial prose
(`SSE_TEXT`) and tool steps (`SSE_STEP`) between tool rounds, Claude-Desktop-style.

## Module map

Where each concept lives in the code:

| Concept | Module |
|---|---|
| Domain models + event envelope | `core/models.py` |
| Layered settings + masked snapshot | `core/config.py` |
| Append-only log / pure fold / SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Sandbox seam + subprocess/Docker bodies | `runtime/sandbox.py` |
| Researcher/Developer roles (toy + LLM) | `agents/roles.py`, `agents/unified_agent.py` |
| Structured output + LLM client + cost accountant | `core/parse.py`, `core/llm.py` |
| Durable per-run observed-usage ledger | `engine/costs.py` |
| Operators (merge/ensemble, sweep) | `search/operators.py`, `sweep.py` |
| Control loop + crash-resume | `engine/orchestrator.py` |
| Authoritative server command lifecycle + leases | `serve/run_commands.py` |
| Variance gate + multi-seed confirmation | `trust/gate.py`, `trust/confirm.py` |
| CV harness, K-fold, purged walk-forward | `trust/cv.py` |
| Leakage detectors + data profiler | `trust/leakage.py`, `core/profile.py` |
| Vector store + agentic retrieval | `tools/vectorstore.py`, `tools/retrieval.py`, `tools/knowledge_tools.py`, `agents/agent.py` |
| Cross-run case library | `engine/memory.py` |
| Part IV/V concept materialization + graph projections | `core/concepts.py`, `search/concept_projection.py`, `search/concept_graph.py` |
| Cross-run index, claims + agent reads | `engine/cross_run_index.py`, `engine/claims.py`, `tools/cross_run_tools.py` |
| Portfolio governance + paid steward lifecycle | `engine/concept_registry.py`, `engine/governance_health.py`, `engine/steward_invocation.py`, `engine/concept_steward.py`, `engine/claim_steward.py`, `engine/task_facets.py` |
| Research Atlas / typed owner governance HTTP | `serve/routers/cross_run.py` |
| Research Atlas UI + evidence validation | `ui/src/ResearchAtlas.jsx`, `ui/src/researchAtlasModel.js` |
| Trace span exporter | `core/tracing.py` |
| Search policies | `search/policy.py` |
| Static HTML lineage tree | `events/htmlview.py` |
| Task adapters + loader | `adapters/tasks.py`, `adapters/toytask.py`, `adapters/regression.py`, `adapters/classification.py`, `adapters/timeseries.py`, `adapters/mlebench*.py`, `adapters/repo_task.py` |
| Strategist / Deep-Research / report | `agents/strategist.py`, `agents/deep_research.py`, `serve/report.py` |
