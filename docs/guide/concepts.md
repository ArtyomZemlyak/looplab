# Concepts

This explains the machinery behind a run. It's the *how it works* companion to the task-oriented
guides. For the full design rationale and decision records, see [`../00-INDEX.md`](../00-INDEX.md).

## The loop

A run is an `Engine` (orchestrator) driving four roles in a cycle:

1. **Researcher** — proposes an `Idea` (an operator + params), reasoning about the goal and prior
   results. Foresight ranks the candidates *before* an eval; the idea also states a one-line
   **hypothesis** that lands on the board.
2. **Novelty stage** — a near-duplicate check on the fresh proposal, selected by `novelty_mode`
   (default **`llm`**). Duplicate detection is the agentic Researcher's call, so the shipped path is
   the LLM one: whenever a client is available an LLM *reads the real prior experiments* and
   adjudicates whether the idea repeats one, then asks for something different **once** — one extra
   LLM call per proposal. `novelty_mode=off` drops even that and trusts the Researcher's own
   read-the-history judgment.

   The **algorithmic** gate (`novelty_mode=algo`, or the legacy alias `novelty_gate=true`, or a
   Strategist novelty stance of `explore`) is the opt-in alternative: **off by default** and *not*
   turned on by `profile=thorough`. It nudges a proposal off a duplicate by numeric param distance
   < `novelty_epsilon` (`0.05`). Its idea-text-cosine ≥ `novelty_semantic_threshold` (`0.92`) arm
   additionally requires `novelty_semantic` (also **off by default**), because embedding similarity
   mis-fires on paraphrases and cannot explain itself — semantic/param search stays available to the
   Researcher as a **tool** that suggests candidates for the LLM to judge.
3. **Developer** — implements the idea as runnable code (or applies params to an existing repo). On
   a fresh repo node it runs three phases — **stages → plan → implement** (see
   [below](#the-developers-three-phases-stages-plan-implement)); a repair is one focused session.
4. **Sandbox** — runs the candidate in isolation with a timeout and output caps.
5. **Evaluator** — scores it (cross-validation, a held-out grader, or a repo's own eval command);
   then the trust gates + optional multi-seed confirmation decide what may be selected best.

The engine then chooses the next macro action through one explicit authority order: the receipt-backed
**Card queue** when `card_driven_selection` is enabled, otherwise the unified-agent **pilot** when
`agent_drives_actions` is enabled, otherwise the configured search **policy**. The cycle repeats until
a budget is hit. At the end, the top candidates can be **confirmed** under multiple seeds, and the best
becomes the **champion**. If confirmation cannot secure a pinned GPU it backs off (0.5s doubling to a
5s cap) and, after **5 consecutive refusals**, appends a durable `pause` — a host that can never satisfy
the pin (a CPU-only resume, a lost driver) stops the run for repair instead of spinning. Fix the runtime
or re-pin the Card, then `looplab resume`.

```
Researcher → Novelty gate → Developer → Sandbox → Evaluator → trust/confirm → Card | agent | policy → repeat → champion
```

### Two clocks: the node count, and occupancy

`engine/cadence.py` holds **two** pacing rules, and they answer different questions.

The old one, `cadence_due(n, last, every)`, is the **node-count** pace behind every periodic
subsystem: lessons distillation (`lessons_every`), the lesson-store refresh
(`lessons_refresh_every`), deep research (`deep_research_every`), the run report (`report_every`),
the Strategist consult (`strategist_every`) and the concept re-tag (`concept_retag_every`). It is a
*since-last* window (`n - last >= every`), not `n % every`, because the node count advances in
strides of more than one — a build fan-out, a seed batch, a merged or failed node — and modulo steps
clean over the only multiple in a window.

The second one, `occupancy_due(inflight, queued, width)`, exists because **a node count cannot
express "an evaluation has been running for four hours and the board behind it is empty."** That is
the state that mattered: an evaluating node is still `pending` in the fold, so the Card queue kept
answering with *its* evaluate action for the entire evaluation — an action the turn cannot start —
and the only writer of Card inventory was reachable only at occupancy **zero**. Across the 52-run
corpus that cost **167.7 GPU-h with no evaluation running at all**. When an evaluation takes hours,
pacing production on how many nodes have finished is pacing on a clock that has stopped.

So production is now also due when a GPU is busy and the supply behind it does not cover the width:

```
inflight > 0  and  (inflight + queued) < max(1, width)
```

`inflight` is the evaluations actually running in this process, `queued` is work already built and
waiting for a slot, and `width` is the settled `eval_parallel`. It is **slots, not wall time** — the
rule reads no clock — and it produces at most `width - inflight - queued` creates, only `draft` /
`improve` / `merge`, only when the turn planned no creates of its own, and only under
`card_driven_selection`. Measured on a toy backend at the shape of a real GPU run: max occupancy
1 → 2, serial gap 18.1 % → 3.4 %, slot utilisation 40.9 % → 79.8 %. Where a prefetch was already
filling the board the two arms are indistinguishable — the pace stands down rather than
double-producing.

It is deliberately **unconfigurable**: there is no `Settings` field for it, because it is not an
interval. Its idempotence is its own condition — producing raises `queued`, which makes it stop being
due — which is why, unlike every node-count pace, **it records no `at_node` mark**. That is the rule
for any third pace as well: a pace that records an `at_node` mark is a node-count pace whatever it is
called, and it would both close the node-count window for a full `every` nodes and make
`already_covered_at` refuse the next firing at the same count. It also reads *live* in-process state
rather than the durable `node_eval_started` boundary, so a freshly resumed process sees occupancy 0,
takes the ordinary create turn, and is right to.

### …and one PRECONDITION both of them share

There is still no third pace, and the reason is worth reading before anyone proposes one. What
broke in August 2026 was not a pace at all: it was the *precondition* every node-count consumer
shared. Five of them opened with

```python
if state.pending_nodes():
    return False
```

under one six-word docstring clause, written once in June 2026 and copied by imitation four times:
**"only at a creation decision point (no pending evals)"**. Under serial evaluation those two things
coincided, so one test served for both. When the eval task group was hoisted to run scope
(2026-08-13) a node stayed `pending` across outer-loop turns, and the observable became false for
the whole life of any evaluation while the requirement — *is the loop at its creation decision
point?* — stayed true. Measured over the runs on this box, prefixes with nodes and none pending:

| run | quiescent prefixes | what the family recorded |
|---|---|---|
| `rubertlite-dr-unified-v6` (to 08-13) | 850, in 5 mid-run windows | fired |
| `rubertlite-dr-unified-v7` | **0** | nothing, ever |
| `rubertlite-dr-unified-v8` | 148, in **one** window | one firing, in the last 8.1 min of 47.6 h |
| `rubertlite-dr-unified-v9` | **0** | nothing, ever |
| `e5small-dr-unified-v2` (live) | **0** | nothing, ever |

v8 is not an older baseline — it started after every commit in that change and its configuration is
byte-identical to v9's — so the difference between them is **run shape**: the family now fires at
most once per run, in the end-of-run drain, and not at all in a run that ends with an evaluation
still going. The blast radius was never only concepts: `strategy_decision` shares the gate, so the
Strategist has adapted nothing since v8, and so do `coverage_snapshot`, `lessons_distilled` and the
serial deep-research and report refreshes.

`cadence.at_creation_boundary(pending, while_evaluating=…)` is the fix, and it is deliberately *not*
a pace: it reads no `n`, no `last` and no `every`, and it records nothing. The interval and the
`at_node` idempotence are untouched, so the number of paid passes per node count is unchanged — the
only new cost would have been the outer loop reaching a gate many times at the *same* `n`, which the
two consumers that record no mark on their "nothing changed" path bound with an in-process
attempted-at-`n` memo. The other two — comparative-lesson distillation and the report refresh —
need no memo, because they record their `at_node` on every path: `lessons_distilled` is appended
even with zero lessons, and the report writer stamps `at_node` outside its own try, so even a
provider failure closes the window.

**Four of the five call it; the fifth is a refusal.** The serial deep research
(`_maybe_deep_research`) keeps the old predicate on purpose. Its phase never stopped happening —
the *concurrent* half of that same decision (`_spawn_research`) carries no such guard, and
`research_completed (trigger=cadence)` is alive in every run on this box, including the three with
zero quiescent prefixes. Opening the serial half mid-eval would put a main-task think and a
background think at the same node count with only a read-then-write window between their shared
mark check and their receipts, buying a double-spend to reach work already being done. The residual
hole — `concurrent_research=false`, not the shipped default — is filed as backlog F1i-b.

`Settings.cadence_while_evaluating` (ON) is the kill switch back, and it carries a
`LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row pinning it `false` for a run resumed from a snapshot written
before the field existed — re-entry must not silently add paid calls to a run whose first half
bought none, and "the new behaviour is better" is the argument that rule exists to refuse.

**What the mid-eval classifier may and may not do.** A concept tag produced beside a running
evaluation is a strictly wider producer than the quiescent-only one the evidence channel was
reviewed against, so it is recorded with an `at_pending` stamp and
`core/models.py::classifier_verified_node_concepts` refuses it. It is a first-class **read-model**
tag — that is the point, since an experiment whose whole authored membership is the run constant
has nothing of its own until the classifier speaks — but it never enters the `graded_novelty`
admission precheck, so no concept can reach selectability. An in-flight pass also records **no**
`concept_consolidation` rename: a rename is the one output of this cadence that is retroactive and
run-wide (the fold applies it backwards to every authored-delta node's membership and every read
surface resolves ids through it), and a per-row stamp cannot express that. Once the run drains, a
quiescent pass re-tags what the in-flight one wrote — bounded by the existing `_RETAG_CAP` — so a
run that reaches a quiet moment ends with exactly the evidence it would have had before.

## Event log = canonical replay state

`events.jsonl` is the append-only source of truth for the **replayable run state**: nodes, metrics,
controls, approvals, terminal scopes, and numeric LLM usage. The engine writes domain effects; the
server writes serialized control intents; and the durable accountant may append `llm_usage` from a
background callback. `EventStore` serializes these writers across processes.

Every row carries an envelope version `v` (ADR-1). A row declaring a version this build does not
support is **undecodable**, not "folded as v1" — reads stop there and the store fails closed, so a
tail written by a newer LoopLab can never acquire today's run or command authority. Upgrade, or use
`looplab repair-log` if the row is genuinely corrupt.

Readers that cache a parsed prefix (the event store, the UI log pager) verify a bounded fingerprint
of the bytes they already read whenever the file changed, so a prefix rewritten **and then appended
to** cannot top a fresh tail onto stale state — growth alone never proved that. Cross-run tools go
further and say so out loud: a sibling run whose log could not be read to the end is labelled
`PARTIAL SOURCE`, because a truncated log folds into a state that looks complete and would otherwise
let an agent read "no later experiment exists" out of "the log stopped here".

Work that costs money or touches the outside world is **receipted before it happens, not after**. A
Deep-Research step appends `research_attempted`, a speculative Card build appends
`card_build_attempted`, and a repo task's one-time `run_setup` appends `run_setup_started` — each
before the provider or the command can be reached. Recording only the *result* made a kill between
"the model answered" and "the memo is durable" indistinguishable from "nothing happened", and resume
bought the same think, the same build, or the same install a second time. With the attempt on disk:

- a research trigger is spent by its attempt, so a think interrupted by a **process kill** is simply
  lost rather than re-paid (ask again explicitly if you still want it). The ordinary case — the
  evaluation the think overlaps finishing first — is not a loss: the receipt, the provider call and
  the memo are one indivisible step, so a think that reached the provider always lands its memo
  before the eval window closes. The receipt is what "spent" MEANS, so the repeat loop marks its
  one-shot trigger used at the same instant the receipt goes down and not when the whole pass
  finishes — otherwise a failure after the memo was already durable made the next tick wear the
  same trigger, write a second receipt and buy the same think again. For the same reason the
  steering a memo produces (the legacy `hint` row, the open-question board rows) is best-effort
  ONCE `research_completed` is on disk: it is derived from an artifact already paid for, its
  loss is logged, and the memo body still carries every direction. A refused `research_completed`
  is not swallowed — with nothing durable, the raise is the only signal there is;
- a Card-build request that carries an attempt from a dead process is **quarantined** — closed and
  moved to the serial path — instead of silently re-issued to a provider;
- `run_setup` is exactly-once for a command that reported an outcome and at-least-once across a kill
  in between, and that repeat is stamped `after_interrupted_attempt` in the log. LoopLab cannot make
  an arbitrary operator command transactional, so prefer an idempotent one.

**The environment a run actually got is a fact on the log, not something to reconstruct afterwards.**
A repo task appends `deps_declared` once at run start: what its source tree declares (the requirement
lines, verbatim), what LoopLab did about it (`installed` / `operator_run_setup` /
`auto_install_disabled` / `refused_untrusted_tier` / `nothing_declared`), the declaration files it saw
and deliberately did **not** act on (`pyproject.toml`, `environment.yml`, lockfiles), and the pip
directives inside the file it does not follow (`-r other.txt`, `--index-url …`) — so the receipt can
never read as "these lines are everything the repo asked for" when part of it points somewhere nobody
looked. `run_setup_finished` then carries `env_delta`: for every distribution the repo declares, the
version before and after. Honouring a pin can move a shared interpreter *backwards* — the version the
repo asked for is not always the newest one present — and that has to be visible rather than inferred.
Beside it, `dropped_requirements` names any declaration LoopLab could **not** honour: the declared
line and pip's own sentence refusing it, one entry per line. A repo whose requirements file carries a
line no reachable index serves is installed without it rather than refused outright (a deliberate,
reversible choice — see `auto_install_deps` in [Configuration](configuration.md) for exactly what
still aborts), and these two fields together are how an operator reads "we honoured 20 of 21
declarations, and here is the one we did not" without opening a log.
`deps_installed` covers the other, narrower mechanism (the crash-time installer for a library a
traceback reports missing) and carries the same detail per package under `resolved`: which requirement
string pip was handed, what the repo declared for it, and the before/after versions. Neither event is
folded; both exist so an operator can answer "what did this run run against?" from the log alone.

Cross-run report generation is paid work over a *set* of runs, so no single run's usage ledger owns
it: its spend lives in the report's own action receipt, written before the report may be published
and before the action may claim success.

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
├── task.snapshot.json    # canonical RESOLVED task (file + CLI flags + comparison contract)
│                         #   → the run is self-describing
├── engine.lock           # live-engine fence (one reducer per run dir; control appends serialize separately)
├── AGENTS.md             # task/contract context file for coding-agent backends
├── .commands/            # durable command records plus execution/activity claims
├── .llm-usage-outbox/    # numeric same-ID usage awaiting/confirming its event append
├── nodes/node_<id>/      # per-node eval workdirs (also confirm/ and ablate/ scratch dirs)
├── tree.html             # static lineage view (regenerable)
├── trace.json            # derived event/trace projection for the UI
├── chat.jsonl            # run-scoped operator/boss transcript sidecar
├── readmodel.sqlite      # derived SQLite read-model, rebuilt from events (+ -wal/-shm)
│                         #   carries a watermark saying which event prefix it covers;
│                         #   `looplab readmodel RUN_DIR [--check]` rebuilds/checks it
├── engine.stderr.log     # bounded stderr of a server-spawned engine (startup diagnostics)
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
lets a reloaded client reattach without confusing one action's result with another. An intent whose
EFFECT is already gone never blocks either of those: it is reconciled first, and if it is still
unresolved after that it is settled history and admits the new command. Command reads and
writes are `no-store`, so an intermediary cannot pin an old `accepted` response.

**No state of the control plane may leave the operator without a move.** Every refusal names a
remedy, and the remedy has to lead somewhere — a pair of refusals naming each other is what made one
run uncontrollable for 30 hours on 2026-08-11, and `tests/test_control_plane_liveness.py` now
searches the reachable state space for another. Two rules follow from it. `pause`'s postcondition is
`paused`, the fold of the operator's own intent, and NOT the engine process exiting: the engine
finishes the node it is evaluating before it releases the run, which on a GPU stage takes hours, so
gating on it made a pause unobservable on exactly the runs where pausing matters. Whether the driver
has released `engine.lock` yet is reported as `engine_stopped` on the succeeded record. And
`POST /commands/{id}/retry` mints a FRESH intent when the run has SUPERSEDED the old one (a pause a
later resume consumed); where it may not — re-issuing an additive intent would apply it twice — it
refuses with `409 command_intent_spent` and names submitting a new command instead of re-driving an
event nothing can observe.

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
process generation is provably alive. The same route resolves an UNREADABLE `cmd_*.json` record,
which is otherwise permanent: a record the server cannot parse counts as active (fail closed, so a
delete cannot erase the evidence of a command whose state is unknown), which refuses every later
command, refuses reset and delete, and answers 503 to the GET its own refusal names. Such a record is
QUARANTINED rather than deleted — renamed out of the `cmd_*.json` glob, bytes intact on disk.

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

A whole-run deletion moves the run into a quarantine directory named for its own operation before
anything is purged, and on the FUSE/S3 mounts runs usually live on that "move" is a per-object walk
rather than one rename. A file created after the walk's scan — CPython's `*.pyc.<n>` bytecode temp,
`tempfile`'s `.tmp*`, a checkpoint a killed process never renamed into place — is simply left in the
source, and the retry then finds both the run and its quarantine. **The retry finishes the move**: it
carries each remaining entry into that same quarantine, never deleting anything and never writing
over a name the quarantine already holds, and removes the emptied directories with `rmdir` alone —
so an entry that arrives mid-walk becomes a refusal rather than a deletion nobody authorized. A name
the quarantine already holds is proof the entry is *not* residue of that move, and it still refuses:
a directory recreated at the run's name with content of its own is never absorbed. What that refusal
now reports is `retryable: false` plus the exact blocking paths, because nothing in the server will
ever touch them and a retry that cannot progress must not ask to be pressed again. The receipt phase
stays `quarantining`; `quarantine_ambiguous` remains the one absorbing off-index state and is not
reused for this.

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
stop-aware `/resume` route remains available specifically to attach a recovery driver.
`/control` now ANNOUNCES its deprecation on every successful append — `Deprecation: true`, a `Link`
naming `/commands` as the successor version, and a `Warning` stating the exact hazard: it has no
durable request identity, so a lost-response retry re-appends an ADDITIVE intent instead of
resolving to the record it already created. There is deliberately **no `Sunset`**, because RFC 8594's
field carries a date and no removal date has been agreed; the header pair is `Deprecation` + `Link`
until one is. The server also tallies who still calls it, by event type and User-Agent
(`routers/control.py::legacy_control_callers`), so the migration is a number rather than an
intention. Behaviour is otherwise unchanged: requiring `expected_seq` here was tried and reverted,
because a silent 409 breaks the compatibility this route exists to provide. Current Web,
boss, and TUI controls use the command lifecycle above. Report regeneration remains a background job,
but its run-generation lease and cost events share the same destructive boundary.
Standalone legacy CLI `stop`, `finalize`, `resume`, and `approve` commands are not yet participants in
the server sequencer and must not be run concurrently with an active server-owned command. Migrating
those direct CLI paths is an explicit compatibility boundary.

### Branching from a snapshot (fork-to-branch)

An operator reading a **historical snapshot** can see the moment they want to branch from. Every
other node action is refused there, and for a real reason: those actions name a node and let the LIVE
state supply the rest (the client reads the node's current `attempt`), so a click made while reading
seq N would execute against whatever the tail happens to hold. Branching is different in kind — the
whole intent travels in the payload — so it is the one gesture a historical view may perform.

It is **not** a new control event, and it is not `fork`. `fork` (`EV_FORK`) means "Researcher,
propose an improvement on this node" and carries no idea at all. Branching with an *edited* idea is
`inject_node`, which already transports an operator-authored `Idea`, a parent, and the
`parent_generations` compare-and-swap that fences it — plus one optional key:

```jsonc
{"type": "inject_node", "data": {
  "idea": {"operator": "improve", "params": {"x": 0.125}, "rationale": "halve the step"},
  "parent_id": 3,
  "parent_generations": {"3": 0},
  "forked_from": {"node_id": 3, "generation": 0, "observed_seq": 412}
}}
```

`forked_from` is the lineage receipt. Its three authored fields say what the branch came FROM and
where the operator was standing; `observed_seq` is bounded by the run's current tail, so it can never
name a state that does not exist. The server then **stamps four more** and refuses a payload that
supplies any of them, so what the record says about authorship is a fact anyone can re-derive from
the same log rather than a claim the browser made about itself. The whole receipt lands on
`node_created` and is folded to `Node.forked_from`.

| stamped field | what it says |
|---|---|
| `base_idea_digest` | the parent's own versioned idea identity, so the lineage is checkable |
| `changed_fields` | every `Idea` field on which this idea differs from its parent's |
| `authored_fields` | the differences the branch puts a VALUE behind — the operator's own substance |
| `not_carried_fields` | the differences the branch is EMPTY at, where the parent was not |

**Why the diff alone is not an answer.** `changed_fields` is a raw comparison of two `Idea`s, and a
branch differs from its parent for two unrelated reasons: the operator edited something, *and* the
gesture deliberately does not carry the parent's engine bookkeeping across (`card_id`, `hypothesis`,
`footprint`, `theme`, the concept envelope — see below). An operator who edits exactly two things
already gets a three-field diff on a toy run, and eight fields against a Researcher-built parent. A
reader shown that as "what the operator changed" is told a falsehood; one shown its complement as
"what the parent contributed" is told a different one. Only the server can split it — the node's
idea *drifts* after intake (the Developer's footprint finalization mints a `footprint` the submission
had none of) and the parent may since have been reset out of the folded state — so it stamps both
halves. The two lists deliberately do **not** partition `changed_fields`: `not_carried_fields` claims
the *parent* had something there, so a field where neither side carries anything and the values still
differ is in neither, and an honest residue beats tidy arithmetic.

**What a later reader may conclude.** `ui/src/forkProvenance.js` is the one place that decides, and
it degrades in named steps rather than guessing: a receipt carrying both stamped halves is
`stamped`; one written before they existed is `legacy` — the *inherited* set is still sound there
(a field the branch carries that the diff does not list holds the base's own value) while "what the
operator wrote" is not, and is left unclaimed; a receipt with no readable `changed_fields` at all is
`unrecorded`, which proves the branch and nothing else. An inherited field is always attributed to
the **parent node**, never to "the Researcher": a branch can be taken from another branch, so the
value's source may itself be operator-authored and this surface cannot tell.

Two places show it. The node card carries a `⑂` chip whose tooltip is the whole sentence, beside the
existing cross-run `⤴` and deep-research `💡` chips — those say where a node was *seeded from*, this
one says who wrote its *idea*. The Inspector prints a **Branched by an operator** block above the
idea and marks the headings over it, so a `Rationale` carried across verbatim reads as *carried over
from #3* rather than as this experiment's own justification. A node nobody branched gains no label
anywhere.

`GET /api/runs/{run_id}/prov`, the W3C-PROV export, carries the same split. A branched node's
experiment activity is `wasAssociatedWith` **two** agents with explicit roles — `agent:operator`
(`prov:Person`, `ll:idea-author`) and the engine's `prov:SoftwareAgent` (`ll:implementer`, because
the Developer really did write the code) — and carries `ll:authored_fields`,
`ll:not_carried_fields`, `ll:carried_over_fields`, `ll:observed_seq` and `ll:attribution`. Before
this, every activity in the graph was associated with the software agent alone, so an experiment an
operator authored was exported as the engine's own proposal with the parent's inherited
`ll:rationale` on it. A node nobody branched keeps exactly the association it always had, with no
role attached.

**The fence is a CONTENT compare-and-swap, deliberately not a tail one.** The legacy `/control`
route binds an *approval* append to the pre-normalization tail, because an approval means "accept the
gate that is open right now" and a replacement request must not be granted by a click aimed at its
predecessor. A branch has no such dependency: it means the same thing at seq N and at the live tail
as long as its named parent is still the one the operator saw, which `parent_generations` already
checks. A tail CAS would be strictly worse than useless here — a live run appends unrelated rows
several times a second, so it would refuse branches whose meaning nothing had touched.

When the run *has* moved, the operator is told so rather than having their branch quietly re-aimed:
a parent that was re-run answers `409 stale parent #3: current generation is 1`, and a tombstoned or
aborted parent answers 409 as well. Nothing is queued in either case.

**That refusal arrives in two different shapes, and a client has to read both.** The legacy
`/control` route answers a stale parent with the 409 above. The durable `/commands` route answers
`200` with a **rejected record** — `{"status": "rejected", "error": {"code":
"command_target_not_found", "message": "stale parent #3: …"}}` — because a command that never
reached the log still produced a durable record saying so. Both are proof that the intake refused
*before* appending anything, which is the only thing that licenses telling an operator "nothing was
queued". A client that reads only the HTTP status would degrade the second one into "outcome
unknown" and invite a second branch for one idea; one that reads only the record's wrapper text
would find the wrapper's message rather than the refusal's. `tests/test_fork_from_seq.py` pins the
asymmetry and `ui/src/forkFromSeqModel.js::classifyForkFailure` is the browser half that reads it.

**Where it is in the UI.** Open a point in the timeline, right-click (or use the node's action
trigger on) the experiment you want to branch from, and take **Branch from here…**. That is the only
item the menu offers in a historical snapshot — every other node action is still refused there, and
they are absent rather than shown and then refused. The panel seeds `operator`, `rationale` and
`params` from the snapshot's own idea and refuses an unedited copy, since that is not a new
experiment. It carries the evaluation profile, timeout and search space across; it deliberately does
**not** carry the concept envelope, the Card, the hypothesis or the footprint — a branch you authored
is not inside the Researcher's Card budget and should not assert a taxonomy you did not write (those
are the fields that turn up in `not_carried_fields`). A field you put nothing in never travels at
all: an untouched form is refused as an unchanged copy, and the receipt does not record you as
having changed something you did not touch. On a
refusal the form stays live so you can fix and resubmit; if the run moved under you it fences and
offers to take you back to live to re-read the parent; and if the outcome is *unknown* — a timeout,
a 5xx — it fences with no retry at all, because branching queues paid work and a second press is how
one idea becomes two experiments. The item is not on a live run's menu: a branch records the vantage
point it was formed at, and a live view has none.

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

That marker cannot cover the part of a build that happens *before* it. `node_building` is appended
after the Researcher's proposal returns, so for the whole of that call — routinely the longest single
wait in the loop — there is no marker, no node and no state field, and every surface fell through to
"Planning next experiment…". The engine therefore also emits a **`phase_progress` beacon** at each
step boundary of a build (`propose` → `novelty` → `reserve` → `implement`/`repair`). It is a
*diagnostic* event: `replay.fold` ignores it by design, because it is transient progress and a resume
must rebuild the same `RunState` with or without it, and because it is appended from concurrent
producers where only a fold-ignored type is permitted (invariant #1).

An **evaluation** had the same hole, one level down and far larger: a build is minutes, an evaluation
is hours. `stage_finished` is folded but lands only at a stage's *completion*, so between two stage
rows nothing could say whether a node was mining negatives, training or scoring — and on a real
`mine` → `train` → `score` pipeline the single label every status surface showed, "Training /
evaluating", is false for two of the three. The engine's own watchdog had the same problem and
guessed: `train_monitor.resolve_stage_log` picks the freshest-mtime log, and its docstring concedes
that "the sandbox's live stage cursor genuinely is unobservable from here".

So the stage loop publishes that cursor itself: one `phase_progress` beacon on the `eval` stage as
each pipeline step begins, and one however it leaves. The step's own name (`mine`, `train`, `score`)
rides as *detail* rather than becoming a phase, because eval stage names are agent-authored and a
phase is a closed word `assert_progress_phase` can refuse. Beside the name rides the **role** the
engine resolved from the manifest through `train_monitor.eval_log_plan` — and that split is the
point: `train` is a slug the agent chose, so a surface may *show* the name but may only *claim*
"Training" from the role. A pipeline that declares no `role: "training"` therefore reads
"Stage `train` · 2 of 3" and not "Training", which is deliberately weaker than guessing from a slug
that would usually be right — `eval_log_plan` refuses the same inference for kill authority, and a
status surface quietly applying a looser rule would let an operator read an engine claim into a word
the candidate picked.

Both halves are closed in the browser by the same rule: a `started` with no `finished` is *live* by
design, so the cursor is closed in a `finally` — an unclosed beacon would report a node that died
hours ago as still training, which is the one failure mode that makes a live signal worse than none.

A **resume** is just as blank as a build was — every line of `Engine._enter_run` runs before the
loop's first turn, so nothing the UI polls has moved — and it deliberately has **no** beacon. The
event log is the wrong channel for that particular wait: the prologue is where the speculation
receipt gate (which pins the log bytes as unchanged when it *rejects* a run), the finalize-scope
reconciliation and the settled-width pins all read the raw log, and beacons appended there changed
which branch the wrap-up handshake took, minting a fresh **paid** finalize scope instead of resuming
the existing one. Making a resume visible needs a channel that is not `events.jsonl` — a run-dir
progress sidecar the server tails is the obvious candidate.

The `(stage, phase)` vocabulary is closed in `events/types.py::PROGRESS_PHASES` and asserted at every
append site by `assert_progress_phase` — a beacon has no reader that fails loudly (the fold skips it,
a UI keyed on an unknown phase renders nothing), so a typo'd phase would ship as a silently missing
signal. Which phases fire is configuration-dependent: `reserve` fires on the serial build path but not
on the batch lane the shipped default width takes, `repair` only on the `debug` operator's
error-feedback branch, and the resume pair only on a genuine re-entry (decided by one `stat`, because
a beacon appended before the prologue's own `read_all` would land inside it).

## Search policies

The policy defines deterministic node-expansion semantics and is the direct selector when both Card
and agent action ownership are disabled. It is pluggable (`make_policy`); the Card selector also uses
the configured policy's legal lanes and budget semantics rather than inventing a separate search tree:

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
- **repair** — on a failure, hand the failing code + stderr back to fix it, **inside the node that
  failed**. There is no `debug` OPERATOR any more: until 2026-08-13 a node whose inline repairs ran
  out got a fresh Debug node to have another go at the same experiment, and that node is deleted —
  along with any `draft`/`improve` that would be one under another name (nothing may be created to
  retry an experiment that just failed). Since 2026-08-12
  **any** of the fourteen `FAILURE_REASONS` is eligible for repair **in place** within the same eval
  (`inline_repair`): `crash`, `timeout`, `oom`, `setup`, `no_metric`, `drift`, `unclassified`,
  `expect_failed`, `check_failed`, `diverged`, `stalled`, `needs_failed`, `not_learning`,
  `check_false_positive` — not only the mechanical three.

  **WHO SAYS WHICH ONE IT WAS, since 2026-08-20** (`engine/failure_diagnosis.py`). The engine
  classifies only what it CAUSED, RAN or MEASURED and remembers doing — its own clock (`timeout`),
  its three watchdogs' kills (`diverged`/`stalled`/`not_learning`), its cross-reader (`drift`), the
  return code of the setup command it ran (`setup`), and its own `stat` of a stage's declared input
  and output (`needs_failed`/`expect_failed`). Everything else is diagnosed by an AGENT that can
  read the dead eval's stage logs AND the code that wrote them, and that must cite the file, line or
  log record it stood on. Two of its answers exist because no out-of-band channel can produce them:
  `oom` (both text rules were deleted, and device-level free memory is sampled after the process is
  gone) and `check_false_positive` — the stage check is ANOTHER MODEL's reading of stdout, so "that
  reading was wrong" is a claim only a second reader can make. Neither admits a metric: both are
  absent from `NEVER_SALVAGED_REASONS`, so they can neither suppress one nor grant one. The engine's own structural answer stays on the row beside it
  (`engine_reason`) and `reason_source` says who chose the word.

  **A diagnosed reason now LEADS the text the Developer repairs from** (2026-08-28). It did not
  until then, and `check_false_positive` is where that cost the most: its directive says *"Read its
  rationale above before you touch anything"* and nothing spliced the diagnostician's verdict into
  the error, so the one kind whose whole point is *"the declared check is wrong — read WHY"* handed
  the Developer only the refusal being disputed. On `rubertlite-dense-retrieval` node 1 the
  diagnosis reads "the run actually reached val recall@100=0.8114 (matching the known-good baseline
  0.81), yet the verifier flagged" it — and that number was nowhere in the repair context. Asking a
  Developer to fix a run whose numbers it has just been told are correct is how a working experiment
  gets broken to satisfy a wrong check. `failure_diagnosis.py::diagnosis_repair_lead` prepends the
  already-redacted, already-capped `reason_summary` for any TRIAGE-sourced reason, in the same
  position the `not_learning` path has always prepended the watchdog's own sentence. It is
  suppressed for an ENGINE-final reason — a fact the engine measured is not a model's account — and
  when the summary is already in the error text, because one finding said twice reads as two
  independent findings agreeing.

  The engine still HANDS OVER what it saw. `engine_observed_facts` states the exit status and
  whether the process wrote anything at all — a fact `_eval_failure_text` surfaced only when stderr
  was blank, so a pod cgroup OOM-kill leaving a `Killed` line used to reach the judge as that one
  word. It states the fact and never the conclusion; a hint phrased as a verdict would be the
  deleted rule wearing a prompt.

  Two consequences worth knowing. `check_failed` is DIAGNOSABLE while the two filesystem contracts
  are not, because a `check_failed` row is written from another MODEL's reading of the stage's
  output — measured, 21 such rows in `runs/` hide at least three different real causes, 16 of them a
  training that never learned. And `oom` is now ANSWER-ONLY: both of the engine's ways of naming it
  were text rules over the candidate's own stderr, both are deleted, so an out-of-memory failure is
  `crash` until something actually looks. `unclassified` is what the engine records when the
  diagnostician was wired, was asked, and could not answer — it repairs blind under a tighter bound,
  never suppresses a salvaged metric, and buys no extra attempts.
  <!-- FIXED 2026-08-13 (mega-review, doc 40): this list said "eight" and omitted the last three — it was
       written on a branch where 8 was correct and merged beside the registry widening without
       reconciliation; the settings table already listed all eleven.
       FIXED AGAIN 2026-08-14: a merge had left BOTH generations of this bullet in place — a
       "**debug / repair**" one naming eleven reasons, directly under a "**repair**" one naming
       eight, three lines after the prose that says the debug operator no longer exists.
       FIXED AGAIN 2026-08-20: the sentence was corrected "eleven" -> "twelve" when `not_learning`
       joined the registry, and the LIST under it was not — so the count and the enumeration it
       introduces disagreed, in the paragraph whose own history is two earlier miscounts. The
       enumeration is now DERIVED by `tests/test_inline_repair_reason_coverage.py::
       test_the_concepts_guide_enumerates_every_failure_reason`, which is why a third miscount
       cannot ship: the words stay hand-written, the SET is re-derived from `FAILURE_REASONS`. -->
  An in-place repair doesn't consume the node budget;
  deeper failures get a structured "reproduce then fix" directive (`deep_repair`).
  **What stops the repair loop is a model, not a heuristic**: the crash-triage model is asked once
  per attempt (the call the loop already made) and is shown this node's whole repair history — what
  failed, what each fix claimed, which files it actually changed, how far the pipeline got — so it
  can answer "I no longer know how to fix this". Since 2026-08-13 **two more signals stop it**, and
  the fixed count stopped being the transition (`inline_repair_attempts` now defaults to `0`): the
  **Developer itself** may answer a repair ask with `(developer stuck: …)` instead of code — a
  first-class outcome rather than another failed attempt — and a **repair critic** is asked, from the
  third repair on, only whether successive attempts are addressing different causes or circling one.
  A count could answer neither question: `rubert-dr-0804` wore 369 distinct error signatures on one
  broken registry, and v6 node 5 halved a batch size three times against an OOM that never happened.
  None of the three may decide *what the result was* — no model's word moves a metric, a champion,
  selectability or a violation.
  **Since 2026-08-20 the same judge also says WHAT FAILED, over half of the vocabulary.** The
  classification splits in two. Eight reasons are **authenticated facts** the engine recorded out of
  band about what *it* did — `diverged` and `stalled` (the watchdogs' `signals`), `timeout` (its own
  clock), `drift` (the cross-reader's refusal), `setup`, and the three stage-contract statuses — and
  those are final: the judge is not asked about one and could not be read if it answered. Three are
  **readings** of the dead process's own text — `crash`, `oom`, `no_metric` — and those go to the
  judge, over a closed vocabulary, on the triage call the loop already pays for. It costs no extra
  call and it is safe for the record by construction: every reason that suppresses a metric
  (`NEVER_SALVAGED_REASONS`) is on the authenticated side, so a judged reason can move a directive
  and never a number. What it fixes: the three readings are matched by
  looking for known strings in the captured stderr, so they are right when a failure says its own
  name there and wrong when it does not. Measured over `runs/`, **26 of the 41 text-read failures in
  the five modern runs were out-of-memory failures labelled `crash`** — `e5small-dr-unified-v3` died
  with three nodes, zero metrics and eight such rows. The measured win there belongs to the marker list beside
  this (`triage.py::_is_torch_oom`, same day), which scans the whole 64 KB `res.stderr` clamp and
  resolves all 26 — **this rung reclassifies nothing further on today's corpus.** What it adds is
  what a marker cannot be extended to: it answers *what failed* rather than *is this string
  present*, so it also reaches `crash` vs `no_metric` and the next allocator nobody has written
  down; it reads the stage log rather than the captured stream; and it can decline an OOM string a
  script printed while CATCHING one. That case is argued, not measured — the split, the refusal and
  the record columns are the enforced part. The durable
  rows now carry `reason_source` and `engine_reason` beside `reason`, so the record says who chose
  the word and never loses the deterministic answer. The **floors** stay underneath: an absolute ceiling of 50 repairs per node — 12 for a
  crash the *rule* path cannot classify, because 50 is the ceiling under a judge that can say "I no
  longer know how to fix this" and there is no judge on that branch — the eval-time
  budget, `systemic_failure_stop`, and the money ceiling. Both the budget and that history are read
  back off the **event log**, so `looplab resume` continues a node's repair chain instead of starting
  a fresh one on top of it. A judge that
  *could not answer* never means "keep going": a dead endpoint stops the node **and** pauses the run
  naming the provider, while a live model answering something unreadable stops only that node (see
  [LLM & agents](llm-and-agents.md#llm-outage-resilience)). The budget has
  to be generous enough for the real case it protects: a repo with a year-stale `requirements.txt`
  legitimately spends several repairs re-paying migrations before it can reach its own research
  question.
  **And what a repair CLAIMED is now checked against what it changed** (`engine/repair_verify.py`).
  Measured across every repair in the preserved runs, about a quarter of the explained ones named a
  concrete change — a file, a flag, a parameter — that their diff does not contain, and 13 changed
  nothing whatsoever and still bought a full re-evaluation of byte-identical inputs. Deterministic
  only, and tiered by how much the engine can prove: an **empty change set** is a fact about bytes
  that no wording can evade, so two of those in a row end in-node repair; an **unmet named claim**
  is derived from text the agent itself wrote, so it is stated in the judge's history as evidence
  and never stops anything on its own. Both ride on the durable `node_repaired` row, so a resume
  continues the streak rather than refunding it.
  **How precise the advisory half is, re-measured 2026-08-15 over every repair on this box:** on the
  two GPU runs the rung has actually graded it produced four `unmet` verdicts and one of them was a
  real broken promise. Two of the three misses are now fixed, and both fixes move the verdict AWAY
  from an accusation the text cannot support rather than toward one. A token the rationale only ever
  used to cite **another** experiment — "node 1's identical mining config (`mining_type=1`,
  `n_negatives=2`) already passed" — is evidence the rationale reasons *from*, not a promise it
  makes, so it can no longer convict on its own; the verdict becomes `unstated`, the one that already
  means "I could not check this", and never `verified`. And a claim written as an **abbreviation of
  the identifier the code uses** — `grad_accum` against a diff that sets
  `gradient_accumulation_steps` — now counts as met. Replayed over all 2,480 preserved repairs,
  exactly three rows move and **no `inert` verdict moves at all**, so nothing about which nodes stop
  repairing changes. The asymmetry behind both choices: a missed discrepancy is worse than a spurious
  one only while spurious ones are rare, and at one-in-four they were not — a line the judge is shown
  that is usually wrong is a line the judge learns to discount. What is deliberately still `unmet`:
  a rationale that names the broken component and then edits a different file. That is a diagnosis
  rather than a promise, but "you said the bug was in X and did not touch X" is worth saying.
  **And since 2026-08-26 the row also says whether the session was CUT SHORT.** `verified` reports
  what the repair did to the tree; it cannot report why a diff was empty, and the two readings —
  "the agent looked and decided no edit was warranted" and "the agent was still reading when the
  clock stopped" — have opposite remedies. Measured over every run here, pairing each
  `inline_repair` session with its own verdict: **12 of the 12 `inert` repairs in the corpus ran past
  `session_time_budget_s` (1200 s), and 0 of the 65 that finished inside it are inert** — so `inert`
  had become an undiagnosed proxy for "ran out of clock". Nothing needed deriving: `tool_loop.py`
  has announced this through its `on_budget` observer since it was written, and no caller subscribed.
  `node_repaired.budget_exhausted` now carries which bound ended it (`time` or `turns`, kept apart
  because only one of them has ever fired here), omitted entirely for a session that finished on its
  own terms — an absent key means "not cut short", not "nobody looked". Additive and fold-ignored;
  `INERT_REPAIR_LIMIT` is untouched, so which nodes stop repairing does not change. The budget itself
  was deliberately NOT raised: the median repair uses 151 s, 13 % of it, and a bound whose effect
  nobody can see cannot be argued about — recording it is what makes "how many truncated repairs were
  one edit away?" answerable from the record.
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
  A *reused* marker never erases the stage's real record: every repair attempt appends its own
  `stage_finished` rows and the fold keeps the informative one, so `train ok / 6900 s` still reads as
  a train that happened. (Before 2026-08-07 only the LAST attempt's rows were written, so a reused
  stage folded to `reused / exit 0 / 0.0 s` and replay could not tell it from a stage that never ran.)
- **Every stage row says which attempt wrote it** (2026-08-17). Because the rows are last-wins by
  stage NAME and an inline repair does not bump the lifecycle generation, a repair leaves the
  previous attempt's rows standing — so `node.stages` could show `train ✗` for hours while a
  repaired attempt was training. The fold now stamps each row with `repairs`, the count of inline
  repairs applied when it was recorded, beside `node.repairs`, the current count: a row whose
  `repairs` is SMALLER is one no later attempt has spoken about, and the UI's strip mutes it and
  says so instead of drawing a live failure (`core/models.py::stage_row_superseded`). A `reused`
  marker ADVANCES the epoch of the record it declines to clobber — a reuse is the later attempt's
  own statement that the result stands — so a deliberately-reused success is never called stale.
  Both numbers are derived by the fold from the log's ORDER, so no event gained a field and runs
  already on disk are attributed retroactively.
- **Optional inter-stage verify** — a stage flagged `"check": true` hands its output to an agentic
  checker (Researcher/Developer) before the next stage runs, so a diverged train can't silently feed
  eval. Since 2026-08-13 the checker answers a **verdict**, not a concern string: a stage dies only
  when the checker NAMES a physical failure from the closed
  `runtime/command_eval.py::STAGE_CHECK_HARD_KINDS` (`crash`, `nan_or_inf_loss`,
  `no_artifact_written`, `silent_fallback`, `loss_unchanged_from_first_step`, plus
  `declared_condition_violated` when the stage declared an `expect.assert`). Anything else — prose, an
  out-of-enum kind, an explicit "I cannot tell" — is `inconclusive`: it is recorded on the stage row
  under its own key and the pipeline **continues**. The old rule was `startswith("OK")` and it
  discarded 46.6 GPU-hours across 21 stages in the shipped corpus, including two 15-hour trainings
  that exited 0 and one node killed for "validation recall (0.79) is below previous best (0.8491)" —
  the quality comparison the prompt has banned twice. Fail-open is safe here because nothing in the
  RECORD rests on it: the deterministic `expect.files` contract has already run and passed before the
  checker is consulted, and the metric still comes from the operator's reader over the protected
  `score` stage.
  Since 2026-08-17 there is a **deterministic floor under the one verdict that is not a claim about
  mechanism**. `declared_condition_violated` is the only hard kind whose evidence is a number the
  trainer prints about itself, and it was measurably unstable: on `rubertlite-dr-unified-v9` node 0
  the same `train` stage, re-run, drew `FAIL declared_condition_violated: training ended at epoch
  14.87, not all 15 epochs completed` and then `OK` on evidence that differs only in training noise —
  and the refusal bought a full re-train, **8,399.9 stage seconds / 2.33 GPU-h**, for the same
  result. A final epoch of `14.87` against `n_epochs: 15` is not an early stop: HF derives its step
  budget from a floored updates-per-epoch and then takes the ceiled number each epoch, so the budget
  runs out inside the last one (measured on that node: 114 steps per epoch against a 1,695-step
  schedule = 14.868 epochs, bar at `1695/1695`). The engine now reads that itself: when the stage
  declared an epoch count, declared artifacts that already passed `expect.files` on disk, and the
  trainer wrote its own end-of-training summary **inside the last declared epoch**, a
  `declared_condition_violated` refusal degrades to `inconclusive` and the row records both readings
  (`check_inconclusive` = what the model said, `check_epoch_reached` = what contradicted it). It may
  only ever ACQUIT — it cannot fail a stage, cannot raise a verdict, and cannot reach the five
  physical kinds — because the numbers come from text the candidate's own script wrote. **A shrunken
  experiment is still refused**: `rubertlite-dr-unified-v8` node 8 (7.99 of a declared 15, after a
  repair cut `n_epochs`), v8 node 9 (5.99 of 10) and v9 node 1 (1.0 of 50) all keep their refusals,
  and all three had reached `100 %` of their own shrunken step schedule — which is why the boundary
  is the trainer's own final *epoch* against the *declaration* and never the progress bar.
  Since 2026-08-20 there is a **second deterministic floor, under the one verdict the checker's own
  window cannot answer**. `loss_unchanged_from_first_step` asks whether the loss moved from the FIRST
  training step, and what the checker is handed is `run.out[-4000:]` of a `run.out` that is already a
  64,000-byte tail clamp — the first step is not in that window and structurally cannot be. Measured
  on `runs/rubertlite-dense-retrieval`: 16 `node_failed` rows carry `reason: no_metric` from a stage
  check and **ten of those nodes were not failures at all** — the operator reset each one, the `train`
  stage came back `reused` at `seconds 0.0` (the very checkpoint the checker had condemned) and it
  scored 0.805–0.8662 against a run best of 0.8835, i.e. 0.91×–0.98× of best. Node 1's `train.log` is
  1,214,400 bytes and runs `loss=33.9 → 13.3` over 11,248 logged points; its last 4,000 characters
  hold **three** of them and all three read `13.3`. A converged curve's tail is flat, and
  flat-at-the-end is indistinguishable from never-moved when the end is all you are shown. So the
  engine measures the trajectory itself over the whole of *this attempt's* stage log — streamed from
  `train_monitor.attempt_byte_floor` (stage logs are opened for append, so a repaired stage's earlier
  curve is not this one's) and reduced by the same `summarize_loss_window` / `summarize_trajectory`
  the live training monitor uses — then does two things with it: the measurement is **handed to the
  checker** in its own prompt (`trajectory_context`, the block the monitor's judge already gets), and
  a `loss_unchanged_from_first_step` refusal it contradicts degrades to `inconclusive`, with both
  readings on the row. The predicate is **moved**, not *descending*: node 22's loss ran 18.9 → 17.6
  and then climbed to 32.6 and plateaued, so its tail read a constant `32.0` — and it scored 0.8147.
  Like the epoch floor it may only ever ACQUIT, and the other hard kinds are out of its reach by
  name, so the four genuinely diverged nodes (`loss=inf`, `nan`, `-1.5e+10`, `-2.35e+08`) keep their
  refusals; a non-finite loss or an explosion anywhere in the attempt withdraws the veto as a second,
  independent refusal. **The asymmetry is stated as a cost**: a wrong "no progress" ended ten nodes
  at 1,570–4,344 stage seconds each with no repair, no retry and no refunded `max_nodes` slot, while
  a wrong "keep going" runs the remaining stages and is caught by the real metric — 65–67 s of
  `score` on those same nodes. **One case no loss-only rule can catch, and it is a different question
  in kind**: node 12's loss fell 0.986 → 0.0195 while validation recall@100 stayed at 0.0028. The
  loss moved, so this rung acquits it — correctly, because "was the loss unchanged" is false. "The
  loss fell and the model still did not learn" needs the objective metric, and the stage check runs
  *before* the protected `score` stage that produces it, so that judgement belongs downstream, where
  it costs one scoring stage and lands in the operator's own reader.

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

The tier has **three** containerized surfaces, not two, and they are built from one derivation
(`sandbox.docker_tier_kwargs`): the generated `solution.py` (`DockerSandbox`), an arbitrary RepoTask
command (`make_docker_wrap`), and the **operator assistant's shell** (`run_command` / `run_tests` /
`git`, `tools/shell_tools.py`) whenever `trust_mode` is not `trusted_local`. So `docker_image`,
`sandbox_memory`, `sandbox_cpus`, `sandbox_readonly_rootfs` and the `hostile` runtime apply to the
chat shell exactly as they apply to an eval. Before 2026-08-15 that third surface passed none of
them: it got the unconditional flags (`--rm --network none --pids-limit 1024 --cap-drop ALL
--security-opt no-new-privileges`) and ran with no `--memory` cap and, under `hostile`, on the
shared-kernel runtime.

Additional safety monitors are off by default. Under the default `trust_gate=audit` they only surface signals;
`gate`/`block` acts only on high-precision signals:

- `redact_output` — adds the high-entropy pass to the stdout/stderr tail redactor. **ON by default
  since 2026-08-15** (unlike the monitors below it), because its false positives were measured away:
  the composition screen took it from 13 of 744 persisted tails — all 13 traceback file paths — to 0.
  Known credential shapes and the operator's own secret env values are masked before persistence
  either way, so this flag is not the on/off switch for tail redaction.
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

**A clean scan is recorded too, and an absent record is never read as clean.** The detectors above
write a `reward_hack_suspected` event only when they find something, so until 2026-08-19 a run whose
every node was scanned clean produced a log byte-identical to a run whose scan call had been deleted —
and identical again to a run with every detector switched off, which is what four of the six
preserved runs on this box actually are. Every evaluated node now also gets a fold-ignored
`trust_scan` row naming **which detectors ran**, a **count** of findings, and the `code_digest` of the
exact bytes they read (the same digest the flagged event carries, so the two join). It carries no
candidate text — it is a receipt about the SCAN, not about the code. `looplab inspect` prints the
run's summary, and the reader (`looplab/trust/scan_receipt.py`) has three answers, not two:

| what the log holds | reading |
|---|---|
| no `trust_scan` row for the node | `unknown` — nobody can say whether anything looked (every log written before 2026-08-19) |
| a row naming no detector | `unscanned` — the engine got there and every detector was configured off |
| a row naming detectors, `findings: 0` | `clean` — these detectors read these bytes and found nothing |
| a row with `findings > 0` | `flagged` — see the `reward_hack_suspected` row beside it for the detail |

An absent receipt reading as `clean` is the one answer the reader will not produce.

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
- **Card-driven selection** (`card_driven_selection`, on by default) — the receipt-backed Card
  queue owns the next macro action instead of the policy/pilot arm. This flag alone both MINTS the
  queue (each proposal lands as durable, selectable inventory — a `card_added` with no owner yet) and
  selects from it; `speculation_depth` only decides who builds the selected Card. The run-start
  record pins this choice, and Card authority wins if `agent_drives_actions` is also enabled. The
  Strategist can shape the separate atomic `card_scoring` treatment (explore/balanced/exploit plus
  bounded novelty and coverage weights); it ranks only Cards that have already passed durable
  readiness and live-anchor checks.
- **Speculative pre-build** (`speculation_depth`, `-1` = AUTO = **on by default** since 2026-08-05) —
  while the current experiments evaluate, the Card the scorer predicts you will pick next can
  *already be built* by an isolated second Researcher/Developer pair. AUTO resolves to the settled
  `eval_parallel` (one prefetch per evaluation lane, clamped `1`–`64`); `0` is the explicit off
  switch. AUTO settles itself back to *off* where a prefetch cannot pay for itself — a build whose
  roles call no LLM (`--backend toy`), a policy other than `greedy`, a run directory with no run id —
  rather than refusing the run the way a spelled depth would. This is the half of the Card lane that
  `card_driven_selection` alone does not buy: speculation needs both, and at depth `0` nothing
  pre-builds. What depth `0` does **not** switch off is the queue itself — minting Card inventory and
  selecting from it has belonged to `card_driven_selection` alone since 2026-08-07, so a settled-to-0
  run still works the board and just builds each selected Card serially. Before that date it did not,
  and an AUTO depth settling to `0` silently reverted the run to `policy.next_actions` while
  `run_started` still recorded `card_driven_selection: true`.
  A prediction that misses is discarded *before* it reaches a sandbox — a
  `node_failed(reason=superseded)` with zero eval seconds — and its node-budget slot is refunded when
  it can prove it never ran. See
  [what blocked speculation from being the default](configuration.md#what-blocked-speculation-from-being-the-default-fixed-2026-08-05).
- **Unified control facade** (`unified_agent`, on by default) — one engine-facing object implements
  Researcher + Developer (+ Strategist/pilot) over stage-specific clients, tools and local contexts;
  it is not one shared cross-stage conversation identity. It can drive the next macro action within
  a *pure legal-action gate* that keeps pipeline discipline. Set
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

### The run-constant half of a membership

Because the delta contract materializes each node's set as *base ∪ delta*, a run whose base is wide
gives every experiment the same wide set, and a concept carried by **every** experiment distinguishes
none of them. Measured 2026-08-17 over `runs/`: `rubertlite-dr-unified-v9` puts 40 of its 48 tag slots
(83.3 %) on five ids shared by all eight experiments, so exactly one tag per node carries information;
`rubertlite-dr-unified-v7` is 16 of 25 (64 %). Both are runs whose tags are **researcher-authored**. The
three runs the classifier pass actually reached (`v8`, `v6`, `rubertlite-dense-retrieval`) have an
**empty** intersection — a classifier tags each experiment on its own evidence and inherits nothing — so
this is a property of the authored-tag regime, not of the corpus.

`search/concept_lens.py::run_constant_split` is the one rule that separates the two halves, and it is a
**projection, not a stamp**. "Constant across the run" is a cross-node property: when node 0 is tagged
nothing is known about node 7, so a writer-side flag would mean *"constant among the nodes that existed
when I fired"* and would say different things about the same concept depending on when it was written.
Nothing new is recorded; no event type, no `RunState` field, no fold change, so every preserved log
folds byte-identically.

It is deliberately **not** `RunState.run_base_concepts`. The base is seeded from the first evaluated
node's authored set and every later delta node inherits it back through the fold, so the base is
self-confirming — the derived intersection can never be smaller than it, whatever the later Researchers
meant. Deriving the intersection also works on a run with no `run_concepts` event at all (`v7` has none
and still has two ids on all eight experiments).

The split is **fail-closed on coverage**: it is a claim about *every* experiment, so it is made only
when every current experiment carries an exact membership and there are at least two of them. One
unclassified node, one inexact materialization receipt or a one-node run yields an empty `run_constant`
plus a reason, and every reader then renders exactly what it rendered before.

Its second output is the population it exists to make visible: **experiments with no distinguishing
concept**, whose whole membership is the run constant. On v9 those are nodes 0 and 4 — and node 0 is the
run's hard-negative *scaling* experiment. Today it wears five chips and reads as classified; named as
having nothing of its own, it reads as what it is, an experiment the taxonomy says nothing about. The
pass that would classify it is the concept cadence, which never fired in that run (see
`docs/BACKLOG.md` §0.12).

Readers: `GET /api/runs/{id}/concepts` publishes it as the additive `run_scope` block (withheld with
`bounded_frame` whenever the frame's own membership projection was capped or torn, so the frame never
names a constant it did not include); the agent tool `node_concepts` leads a node's line with its own
concepts and names the run's once; the DAG's on-node chips order the experiment's own first and mark
the run-wide ones. All three annotate and withhold nothing — every id still appears.

When `card_driven_selection` is on (the default), a proposal reaches its node through a **native Card**
rather than straight to `node_created`, and the Idea the build executes is rebuilt from the durable
`card_added` action alone. So the whole authored concept envelope rides along on that row — the four
members of `CARD_IDEA_CONCEPT_FIELDS` (`concept_mode`, `concepts`, `concepts_added`,
`concepts_removed`), which are exactly the idea-block keys the card ownership digest
(`CARD_ACTION_DIGEST_V2_FIELDS`) does **not** cover, so tagging never changes an action's identity and
no already-minted Card is invalidated. A **full** proposal's set is decoded into `Card.concept_tags`
with a `kind="card_added"` concept source; a **delta** proposal's row carries the mode and both operand
lists and claims no membership, because a delta is a delta against an inheritance base (the run base at
a root, else the union of the node's parents' effective sets) and that base only exists in folded state.
The claim rebuilds the Idea with whichever envelope the row recorded, so a Card-built node folds through
exactly the same `_materialize_concept_deltas` post-pass as an unmediated proposal — the delta is never
resolved at mint time, which is what keeps the durable log folding to the same memberships on every
replay. Until 2026-08-12 the row carried no membership at all and every Card-built node was created with
no concepts; until the delta half landed the same was still true of every non-root proposal, since the
Researcher authors a delta whenever a parent membership exists.

A **repair attaches to the card it repairs** — historically. A card is a work item that can carry
several nodes, so a `debug` re-attempt of a failed node claimed that node's own card (one more `node_building`, no second
`card_added`) instead of minting a new one. The rule is narrow and fails closed: only `debug`, only one
parent, only a live singly-registered native owner, only a **failed leaf** (the parent node is
terminally `failed`, and nothing else under that card is still pending or building — a question already
being re-attempted does not need a second simultaneous owner), and only when the two seed statements
share a `belief_id` — so a repair that genuinely re-scopes its question still gets its own card, and two
different actions that merely reuse formulaic wording are never merged. Since F5 deleted the Debug
node (2026-08-13) nothing reaches this rule at all — it is kept fail-closed, as the gate a
reintroduced retry operator would have to land on rather than sail past. Before 2026-08-12 a retry
reused the parent's Idea verbatim with only `operator` flipped, which is a different action digest and
therefore a second card whose statement was byte-identical to the first: the board showed one research
question twice. `Card.belief_id` / `Card.retry_of` still name that relationship for any card that does
mint, and every pre-existing log folds unchanged.

A **discarded proposal is receipted.** `_plan_native_card` answers with one of five dispositions —
`mint`, `reuse`, `attach`, `duplicate`, `invalid` — and until 2026-08-27 only `invalid` left a row
(`novelty_rejected{kind: "card_contract"}`). `duplicate`, which is what a BUSY BOARD produces when an
existing card already owns the action, returned `None` two lines below it with nothing written; so did
the third branch, which fires when an accepted disposition comes back with no `card_id` or no `idea`.
All three unwind through `audit._discard_node_build_telemetry`, which despite its name appends no
event — its body only nulls the per-role prediction attributes (`last_hyp_priority`, `last_foresight`,
`last_foresight_pick`, `last_report`) so a later build cannot emit an abandoned build's ranking.

The cost of that silence is measurable. On `runs/e5small-dr-unified-v8` a propose that ran **24.1
minutes, 81 provider calls and 4,270,000 tokens** emitted a well-formed one-knob delta — raise
`train.max_seq_length` 128 → 256 to match the eval's document truncation, citing four `file:line`
locations — and left no `card_added`, no `card_enriched`, no `hypothesis_added`, and no
`card_dropped` anywhere in the log. The next propose spent 148 calls and returned a *different*
hypothesis, so the idea was not recovered; it was lost, and nothing in the record said so.

The receipt lands in **exactly one place**, and which one is the load-bearing part.
`_prepare_node_idea._link` runs immediately after the proposal call and nowhere else, so it is the
only pass that can know a *paid* proposal was refused; it writes `novelty_rejected` with
`kind: "card_duplicate"` or `kind: "card_unplannable"`, `pass: "planner"`, the `disposition` that
produced it, and the proposal's `hypothesis` bounded to 400 chars — the same bound the fold applies
to a `rationale`, so an audit row can never outgrow what is kept beside it.

`_reserve_node_build` stays **silent**, deliberately. It is also the batch pre-reservation entry
point, called with a ready-made Idea and no propose behind it, so calling it twice with the same idea
is the idempotent retry of one action — and
`test_card_writer_lifecycle::test_batch_prereservations_mint_on_main_thread_and_dedupe_exact_active_work`
pins that such a re-reservation appends nothing at all. A row there would count a phantom loss on
every exact twin. (A first version of this change did append there; that test caught it.)

`novelty_rejected` rather than a new event type, because the fold appends it to `st.novelty_events`
with no schema switch, so a new `kind` is additive under invariant #5 and every existing reader
tolerates it.

**Refusing the mint is unchanged and right** — a card whose owner is in flight must not be minted
twice. What changed is that refusing in silence is no longer possible, so the cost is countable and
the idea recoverable by a reader instead of re-derived by a later paid propose.

Attaching is **opt-in at each build site**, and the ordinary build spine is the only one that opts in.
An operator `inject_node` never attaches: an attach writes no `card_added`, so it would discard both the
`source: "operator"` receipt and the `implementation_ref` that binds a human's ready-made code. Nor may
an attach ever CLOSE the card it joined — its `node_building` claim records `card_attached: true`, and a
reservation that is interrupted or fails drops its card only when it minted it. Without that, one
process kill between `node_building` and `node_created` dropped the *parent's* card, evidence and all.

The **proposal prompt shows both halves of the board.** Untested beliefs are the claimable queue (the
model returns a `CARD_ID` and the engine restores the immutable seed); the questions that already have
a live experiment — running, failed or evaluated — are listed separately as read-only context, grouped
by belief, because their work item is already owned. Dropped and abandoned cards are not listed: a
deliberate abandonment is history, not a claim on the direction. That second list is explicitly **not
claimable** — a `CARD_ID` returned from it is ignored, and the decision to re-attempt a failed
experiment is the engine's own (the `debug` attach above), not something a proposal can ask for. The
same brief also feeds the crash-triage judge and the macro-action chooser, whose replies are a verdict
and an index; they see the board's content without either claim contract. Until 2026-08-12 only the
first list existed, so a card disappeared from the Researcher's view the moment it got a node,
including a node still running.

**The Researcher can now RECORD a question it is not pursuing** — `Idea.open_questions`, with
`Idea.question_concepts` aligned by position, both carried on the emit schema the proposer reads.
Until this field only deep research and the operator could put a question on the board: the
Researcher could ANSWER a direction (`parent_card_id`) and, since `read_questions`, READ the board,
and had no way to ASK. A question noticed mid-proposal and left in `rationale` prose is read by
nothing. It is an **output field and deliberately not a tool**, because the engine is the sole writer
of domain events (invariant #1) — and `EV_HYPOTHESIS_ADDED`'s membership in `BACKGROUND_APPENDABLE`
does not license a tool-thread append, since that membership exists for the concurrent research task
whose safety argument is "appending *fewer* rows moves no reader's position".

Registering a question is **free**: the field is in no digest, so two proposals differing only in the
questions they file are the same executable action — a Researcher that had to spend its proposal to
record a question would record none. It is also *tolerant where the concept envelope is strict*: a
malformed value heals to empty instead of raising, because a question is worth strictly less than
the experiment carrying it, and the opposite choice is what discarded two complete deep-research
passes over one flat list. A blank statement KEEPS its slot in the payload — position is the join —
and is dropped only by `question_concept_rows` (after its index is read) and by
`admit_research_beliefs` (from the board). **The engine-side append is not wired yet** — the open-item
marker for it is declared once, on the field itself in `core/models.py`, and the reason it is staged
rather than inlined is that `EV_HYPOTHESIS_ADDED` is FOLDED: appending it inside a reservation's
authority CAS window moves `speculation._proposal_authority_seq`'s max-seq compare and discards a
proposal the run has already paid for, which is the hazard invariant #1 records for
`train_monitor_alert`.

**A DIRECTION IS NEVER A CLAIM, and since 2026-08-26 that is enforced rather than only asked for.**
`agents/roles.py::bind_idea_to_board_card` resolves two independent edges against the same visible
board — `card_id` (a claim on a work item) and `parent_card_id` (a filing under a question) — and
until then a direction could become either one. Both resolution paths reached it: a proposal naming a
`DIRECTION_ID` in `card_id` bound to it (and had its own `hypothesis` overwritten by the direction's
broad seed statement), and so did the SEED FALLBACK, which is the path that fires on a **compliant**
proposal. The direction block asks the model to propose an experiment that moves the direction
forward and file it with `parent_card_id`; a model that does exactly that and echoes the direction's
wording as its own `hypothesis` matched the direction, and the self-edge guard below then saw
`parent.id == chosen.id` and nulled the parent — turning a correct filing into a claim on the
question and destroying the direction→experiment edge. `chosen` is now nulled for a direction after
both resolution paths and BEFORE the self-edge test, which is what lets the parent survive. A
direction named in `card_id` is nulled and deliberately NOT re-routed into `parent_card_id`: the
prompt already says which field to use, and inferring the filing would mint a link nobody authored.

**And since 2026-08-26 an agent can ASK for the board, not only be shown it.** `read_questions`
(`tools/question_board.py`) returns each open research question with its concepts, the experiments
filed under it, and what each of those measured. **It shipped BROKEN and the failure is worth
knowing**: the provider defined `call` where `tools/_base.py::ToolProvider` requires `execute`, and
that protocol is STRUCTURAL — its own docstring says "no provider inherits this" — so nothing checked
it at import or at construction. The first run that loaded the provider lost its whole deep-research
stage on the first dispatch, its memo reading `(deep research unavailable: 'QuestionBoardTools'
object has no attribute 'execute')` with zero findings, zero directions and zero questions. Ten unit
tests passed throughout, because every one of them called `.call(...)` — the name the object defined
— so they confirmed the author's naming rather than the contract's. `tests/test_tool_provider_contract.py`
is the guard that can see it: a class-wide scan where anything offering `specs` owes `execute`, plus
a driven dispatch, since `hasattr` is satisfied by the wrong name being right — the same join the operator's Research ladder
renders. Before it, a census of the whole tool surface found 83 tools and not one that read the
questions: the concept tools read the TAXONOMY and `read_research_memo` reads the memo that PRODUCED
a question. The only channel was the PUSH block below, which is bounded by whatever the brief chooses
to include and reaches only the roles the engine pushes it to.

The role that was blind is not the obvious one. `RunTools` — `list_experiments`, `read_experiment` —
is built for the RESEARCHER only; the Developer's scout set had no reader for the board at all, and
the `read_run_experiment` calls visible in its `stages`/`plan`/`card_build` phases are a FOREIGN-run
reader. So the role writing an experiment's code could not see the question it answers, and the
repair path could not see whether a sibling experiment under the same question had already hit the
same wall. It is wired to both the Researcher's providers and the Developer's scouts as a NARROW
provider rather than by granting `RunTools` wholesale, which would also hand over `read_code` and the
rest. It records nothing: every field is already on the Card, and the fold is untouched.

**So does the deep-research memo prompt**, which is the stage that fills the board: both halves render
from one shared block (`agents/roles.py::board_prompt_lines`), in the same `CARD_ID`/`BELIEF_ID`/
`SEED_STATEMENT_JSON` spelling, without the claim contract (a memo has no `card_id` field). Until
2026-08-12 it saw none of it — four memos in one 90-minute evaluation registered 18 belief rows for
about five ideas, three of them re-wordings of the question whose experiment was running while they
were written. The prompt half is paired with an engine-side bound at the append site
(`engine/research_cadence.py::admit_research_beliefs`): a direction whose case- and
whitespace-normalized statement already names an open belief is not registered, and the open belief
board is capped at five distinct rows — the same window the prompts can show. Everything refused is
still recorded **in the memo body** — `read_research_memo` renders the directions in full — and what
is refused is only the board row. The standing `hint` is a bounded PUSH and not the record: it
carries the first `DEEP_RESEARCH_HINT_DIRECTIONS` (5) directions, so a memo that returns more leaves
the rest out of it. That distinction was documented the other way round until 2026-08-26, when
`runs/e5small-dr-unified-v7`'s third memo — 8 directions, the only one of that run's three with any
content — put three of them in neither the hint nor the board. The bound is deliberately not raised:
the hint is spliced into a prompt and `agents/hints.py` filters on its prefix, so a push that grows
with whatever the model returned is how a brief becomes a wall of text.
Near-duplicate *wording* is not caught here on purpose: over that run's statements a token-overlap
rule scores its best pair across two genuinely different experiments, so paraphrase identity stays
the agentic consolidation cadence's job.

**A question is filed under ITS OWN concepts**, and the positional join that does it
(`engine/research_cadence.py::question_concept_rows`) is one shared pure function rather than a rule
each caller re-spells. `question_concepts[i]` describes `open_questions[i]`, so a blank statement is
skipped **after** its index is read: filtering blanks first and enumerating the shortened list gave
every question after a blank its predecessor's row — driven with `["", "q2"]` against
`[["loss/contrastive"], ["training/negative-mining"]]`, `q2` was filed under `loss/contrastive`. That
is not a mislabelling but a misplacement, because a question's concept SET is its position in the
question lattice. It has never fired on this box, and the zero is evidence rather than comfort: of
173 memos carrying an `open_questions` list, **0** contain a blank and **0** carry
`question_concepts` at all — the field could not reach the durable row until `_assemble` stopped
raising on it, so repairing that carrier is exactly what makes this reachable. A short, missing or
non-list row still yields no concepts for that question, and a question with none is registered
exactly as it was before any of this shipped.

**A question may also sit under a BROADER question**, and until 2026-09-02 it could not: every
`hypothesis_added` row on `e5small-dr-unified-v12` carried exactly `[at_node, concepts, source,
statement]`, while `Card` had carried `parent_card_id` and `child_card_ids` the whole time. The
model was permitted a tree it had no way to describe, and the only edge any prompt asked for was
experiment -> direction. `question_parents[i]` names the parent of `open_questions[i]` — the twin of
`question_concepts`, resolved by the twin function, with the same order rule for the same measured
reason. The path is five links long and each one is a place the feature could ship inert:

    _MemoOut.question_parents          the EMIT schema; its `description` is the only channel in
      │                                front of the model when the tool call is constructed
      ▼
    sanitize_research_memo_payload     builds its OWN dict — a key it does not know is dropped, which
      │                                is how question_concepts once recorded "no concepts" about
      ▼                                memos structurally unable to hold any
    ResearchMemo.question_parents      the carrier; a schema asking for what the memo cannot hold
      │                                ships the feature inert
      ▼
    question_parent_rows(...)          RESOLVES: the exact statement of another question in THIS
      │                                memo (through hypothesis_id, the board's own content
      │                                address) or an id already on the board. Unmatched -> NO EDGE.
      ▼                                Cycles closed inside one memo are dropped whole.
    hypothesis_added.parent_belief_id  the durable row; absent leaves the key out entirely
      │
      ▼
    _on_hypothesis_added -> Card.parent_card_id -> _apply_card_lineage fills child_card_ids

A wrong edge is not recoverable and an absent one is, so nothing is ever fabricated: a parent naming
neither a sibling statement nor a live board id yields a question with no parent, exactly as before.
Self-edges and cycles are refused **once**, by `_apply_card_lineage` steps 2-3, which resolves
aliases first and peels cycles exactly; two earlier duplicate guards (one in the resolver, one in
the ledger) were deleted because no mutant could kill them — a guard no test can fail is not a
guard.

The board read behind `known_ids` is its own hazard and cost a near-miss: `_record_research_steering`
runs on the concurrent research task and is handed the memo, not the state, so reaching for `state`
raised `NameError` **inside the projection's own try/except** — one log line, and every question
registration for that memo silently vanished. `tests/test_memo_questions_reach_the_board.py` caught
it as `(0, 1) == (2, 1)`. `_board_card_ids()` now folds for itself, best-effort: a fold that fails
yields no board ids and a question registers with no parent rather than not registering at all.

**The carrier had a SECOND blockage and it was the ENCODING**, found live on the run launched from
the fix above. That run's first memo came back rich — 10 findings, 11 claims, 64 sources — and the
console read `deep research: emitted memo kept, 1 field(s) refused for shape: open_questions`, so
the model answered the question half of the prompt and the engine dropped exactly that answer. The
emit call's own arguments survive on a `generation` span's `tool_calls[].arguments` (the emit is
not traced as a `tool` span, which is why no `tool` row shows it), and the shape is
`"open_questions": "[\"Does training the e5-small backbone past the 1-3 applied epochs …\", …]"` —
a `str` holding a JSON array of strings where the schema declares `list[str]`. The structure was
right and the quoting was not.

`agents/deep_research.py::_decoded_json_list` decodes it, on any list-annotated field of the emit
schema, derived from `model_fields` so a field added later inherits the tolerance. It fails CLOSED
at two points — the value must be a `str` and the decode must produce a `list` — and healing runs
BEFORE validation, so the durable row only ever holds the declared type and there is no second
spelling to read back. That is what separates it from the shape deliberately NOT healed: an
`[{"question": …, "concepts": […]}]` form would need someone to decide which key is the question,
and a guess like that is how two spellings of one field enter a durable row. A decode is not a
guess. Note the span record clips arguments at `core/tracing.py::_TRACE_TOOL_ARGUMENT_CAP` (16,000
chars), so that memo's `recommended_directions` and `question_concepts` sit past the cut and
nothing is claimed about them here.

**The `assert` EXAMPLE the Developer is shown is load-bearing, and the shipped one was measured
wrong.** Reading `looplab_stages.json` out of every `node_created` row, 33 agent-authored
`expect.assert` strings carry a numeric threshold and **about 28 are the same sentence** — *"hard
negatives mined for at least 90% of the training queries"* — across six independent runs. That is
not invention converging: it was the worked example in BOTH channels, `_stages_user` and the
`declare_stages` tool schema's `assert` description. The model copied what it was shown.

On this data the bar is wrong by more than 2×. `add_negatives` inner-joins mined ids to product names
and drops the rest **by design**, so the real figure is 41.8 % (908,121 of 2,170,069) and the champion
(0.7934) was trained on exactly that — the shipped example refused the recipe that produced this box's
best result. Verified on `e5small-dr-unified-v8` node 1: it mined a valid 2,732,976-row parquet,
failed its own gate, and was abandoned after two repairs, with the engine's own diagnostician
returning `check_false_positive` and being right.

**The replacement is a different KIND of claim, not a smaller number.** "Every row has its
`n_negatives`" is a property the stage CONTROLS; "90 % of queries survive a downstream join" is an
OUTCOME of the data it does not. Both channels now carry the property-shaped example, and
`_stages_user` states the rule outright — assert what you control, PRINT what you do not, and if you
genuinely need a bar on an outcome, measure it first and say what you measured it against — because
an example alone is what got copied last time. `validate_stages` is deliberately NOT changed to
reject thresholds: the distinction is semantic, so a syntactic refusal would either miss the bad bars
or kill the good ones, and a stage that mines 1 % must still fail loudly.

**A repair that DESCRIBES an edit it never made is bounced once, inside the session.** Measured on
`runs/e5small-dr-unified-v8` node 1: two inline-repair sessions spent 51 minutes and 108 tool calls
(`read_file` 50, `run_probe` 26, `grep` 21) with **zero** `edit_file`/`write_file`/`declare_stages`,
then emitted *"FIX: changed mine_stage.py … Updated looplab_stages.json expect.assert to match."*
`node_repaired.changed` was `[]`. The diagnosis was right — it matched the engine's own
`check_false_positive` — and only the application was missing; two such attempts hit
`INERT_REPAIR_LIMIT` and abandoned a node holding valid mined negatives. The wiring was checked and
is fine (the repair path composes the write tools), and the probe was ruled out (0 of 26 probes
contain a write).

`engine/repair_verify.py::repair_claimed_without_writing` is the rule and
`adapters/repo_developer.py`'s repair session is its one caller, through the same `validate=` seam
`_declare_stages_phase` uses to bounce a malformed manifest. It fires only when BOTH halves hold —
the write tool's own ledger shows nothing added, changed or deleted this session, AND the summary
names something concrete a diff could have contained — so an honest *"no code change is needed"*
answer is left alone, which matters because refusing to edit is sometimes correct. It is **one-shot**:
a second bounce would spend the session arguing instead of editing.

**It can never reach the `inert` verdict, and that separation is the point.** `inert` stays decided
on bytes with the rationale unread, because it is the only verdict the loop acts on and a
text-derived signal that could move it would let a model write its way out of `INERT_REPAIR_LIMIT`.
This rung reads the text *inside* the session, where steering is the entire purpose, and touches no
durable record.

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
the same frame does not repair them. Inspect the run's **Lab → Events** and **LoopLab → Knowledge &
prompts** to identify the broken
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
which the classifier re-tag cadence **must not clobber** (order-tolerantly, invariant 5). In the UI this
is the **Edit tags** control in the Inspector → Overview (offered only on a live run with an authoritative
concept projection; a partial/unavailable projection stays display-only so a fabricated set is never
overwritten). Generic node resets
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
On a mixed-era run, that legacy fallback may still group the Lineage DAG while the Concepts view remains
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
  runs. See `looplab/search/concept_analytics.py`.
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
- **Lineage** adds canonical breadcrumb chips over the lineage DAG. Chips are sorted by canonical ID so live
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
result of each run is retained as a source contribution to a direction-scoped **case** champion; at run end `reflection_priors`
(also on by default) distills a causal **meta-note** ("why the winner won"), generalizable
**lessons** (good *and* bad, with a verdict + evidence count), and reusable **skills** — all stamped
with a task fingerprint and matched into the next similar run's proposal prompt. Duplicate lessons
are merged (exact-hash **plus** a hybrid-retrieval → agentic paraphrase-merge pass); the in-run
**research board** stores Card work items and groups them by `belief_id`; the distinct-belief view is
deduped the same way and prioritized by foresight. See
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
from the validated retained capsule snapshot, but deliberately limits co-occurrence pairing to the top 512 map
nodes before pair materialization. Its pair receipt distinguishes response-capped pairs known within that
projection from pairs touching pruned nodes, whose count remains explicitly **unknown** rather than reported
as zero; capsule-source completeness is a separate receipt, merged at the tool because the map fold
(`search/concept_lens.py::project_concept_map`) takes concept SETS and cannot know how complete the rows
behind them were. That fold is shared with the run list's `Concepts` view, which passes the live per-run
rollup of the runs it is showing instead of capsules — one map rule, two populations, each named by its
caller. Because the fold takes no governance, the CALLER canonicalizes — and the browser caller gets
that from `GET /api/cross-run/concept-policy`: `canonical` (`id -> canonical | null`, alias chains
already resolved, `null` = purged, an absent id = itself) plus `split_sources`, the ids whose
canonical form genuinely depends on each run's own siblings and which a client must therefore
declare UNAPPLIED rather than re-derive. Without it a merged pair kept drawing two nodes and kept
reading as spelling drift; with it a governed pair collapses to one node and leaves the drift
report by itself, so what remains listed is exactly the residue nobody has ruled on. The same
response returns `capsule_run_ids`, because the two populations above differ in practice and
nothing else says so: on the development corpus the map draws 15 tagged runs while durable memory
holds 3 capsules. The `is_a` relation has no edge list: a concept id spells its own ancestry, so the tree IS that
relation. A pair below the `min_cooccurrence` floor (default 2 distinct runs) is counted and reported rather
than dropped, so an empty pair list reads as "nothing repeats yet" and never as "nothing co-occurs". Typed
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
families mean it is not yet the production 50–500-run system. The wired owner `#/claims` route is explicitly a
bounded read-only preview of these projections, not that full system.
The advisory `concept_card` lookup reuses exact/fuzzy slugs, keeps scoped track record separate from the global
observation count and frames persisted text as untrusted. It is not a prose-paper generator, verifier,
governance mutation or complete applicability receipt.
The target CR0–CR3 design adds the full applicability/coverage frame, durable derivation contracts,
incremental summaries and an interactive research index; see
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
Installing the `[otel]` extra and explicitly selecting OTLP (`OTEL_TRACES_EXPORTER=otlp` or an
`OTEL_EXPORTER_OTLP(_TRACES)_ENDPOINT`) sends the same spans to any OTLP collector (Jaeger / Tempo /
Honeycomb) with no code change. Spans are diagnostics only — `replay` never reads them.

**What "the trace is complete" means at a reader boundary.** Span export is asynchronous, so before any
derived artifact is built (`trace.json`, `tree.html`) finalization raises a barrier over everything the
exporter has accepted — including a row already handed to the writer, not just the ones still queued. The
barrier settles each accepted row's *one* write attempt (retrying an ambiguous append could double-export a
span), and a row whose attempt failed is **counted, not hidden**: the same barrier emits a
`looplab.exporter.loss` span carrying the dropped/failed deltas, which `trace.json.summary` sums into
`dropped_spans` / `export_failures` / `exporter_loss_receipts`. So an operator reading a trace is never
looking at a silent hole — a missing span is always accompanied by a receipt saying how many are missing.

**Per-operation traces.** A node's own work (propose → implement → repair, then evaluate/training)
is one trace, shown under the node. But every OTHER LLM sub-operation runs in its **own** named trace
(`new_trace`) — `strategist_consult`, `hypothesis_merge`, `deep_research`, `report`, `lessons_distill`/
`lessons_refresh`, `card_build` (the Card-speculation producer — it has no node yet, so it is
run-level by construction, and until 2026-08-05 it opened no span at all, which made the whole
speculative Developer call invisible to `spans.jsonl` and to `looplab timings` while the cost ledger
still billed it), and the two Researcher ranking steps — `hyp_prioritize` behind `hypothesis_ranked`
(board prioritization) and `foresight_rank` behind `foresight_selected` (idea predict-before-execute). The event
that operation emits is **stamped with that trace's id** (the event store reads the active span's ids
on append; a telemetry event whose op-span already closed carries the captured id explicitly), so the
UI expands that event's row to ONLY that operation's trace — never the whole node's Researcher+Developer
tree. `GET /api/runs/{id}/trace/by_trace/{trace_id}` returns one operation's span sub-tree and
`…/by_trace/{trace_id}/conversation` the same linear reading the node conversation gives, so an
operation with no node — a Researcher proposal carries no `node_id` at all — is readable and not only
inspectable; the node's full trace is at `/api/runs/{id}/trace`. Events with no LLM (e.g. `coverage_snapshot`, deterministic)
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
`running (training)` with no live pulse. The Dock's status strip goes one level finer, from the
`phase_progress` beacons above: it names the STEP ("Proposing 4 experiments…", "Writing code for
experiment #7…", "Experiment #5 training / evaluating · train 2/3…") rather than only the fact that a
build or an evaluation is running,
and its age clock measures the current *phase* rather than the whole build — so `40m` beside "Writing
code for experiment #7…" means the Developer has been going forty minutes. The two lanes are
COMPOSED, not ranked: the strip used to return on the first build it found, so on any run wide enough
to build and evaluate at once — the shipped default — every evaluating and queued node was invisible
in the one surface that claims to say what is happening now. The decode is the pure
model `ui/src/buildingModel.js::openPhases`/`livePhase`/`phaseLabel`; `Dock.jsx` keeps only the choice
of which label to show. A resume still shows only the transport strip's "Resume requested…". The assistant chat streams the same way — interstitial prose
(`SSE_TEXT`) and tool steps (`SSE_STEP`) between tool rounds, Claude-Desktop-style.

## Module map

Where each concept lives in the code:

| Concept | Module |
|---|---|
| Domain models + event envelope | `core/models.py` |
| Card identity: versioned action/idea digests, ownership receipts, `Card` + its provenance | `core/cards.py` (re-exported through `core/models.py`) |
| Layered settings + masked snapshot | `core/config.py` |
| Append-only log / pure fold / SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Derived Card ledger (fold-time receipt bounds + the `derive_cards` post-pass) | `events/card_ledger.py` |
| Sandbox seam + subprocess/Docker bodies | `runtime/sandbox.py` |
| Researcher/Developer roles (toy + LLM) | `agents/roles.py`, `agents/unified_agent.py` |
| Structured output + LLM client + cost accountant | `core/parse.py`, `core/llm.py` |
| Durable per-run observed-usage ledger | `engine/costs.py` |
| Operators (merge/ensemble, sweep) | `search/operators.py`, `sweep.py` |
| Control loop + crash-resume | `engine/orchestrator.py` |
| The two pacing rules (node-count `cadence_due`, occupancy `occupancy_due`) | `engine/cadence.py` |
| Authoritative server command lifecycle + leases | `serve/run_commands.py` |
| HTTP control-payload validation (`normalize_control` + the five per-event tables) | `serve/control_validation.py` |
| Durable whole-run Replay/deletion receipts + the destructive-quiescence ladder | `serve/durable_op.py`, `serve/reset_transaction.py`, `serve/deletion_transaction.py` |
| Serve-side paid work: metering lease + claim→terminal receipt ledger | `serve/paid_work.py`, `serve/paid_ledger.py` |
| Variance gate + multi-seed confirmation | `trust/gate.py`, `trust/confirm.py` |
| CV harness, K-fold, purged walk-forward | `trust/cv.py` |
| Leakage detectors + data profiler | `trust/leakage.py`, `core/profile.py` |
| Vector store + agentic retrieval | `tools/vectorstore.py`, `tools/retrieval.py`, `tools/knowledge_tools.py`, `agents/agent.py` |
| Typed tool capabilities/results, MCP structure/cancellation, and operator-pinned Developer commands | `tools/_base.py`, `agents/tool_loop.py`, `tools/mcp_tools.py`, `tools/dev_commands.py`, `engine/workspace_seed.py` |
| Cross-run case library | `engine/memory.py` |
| Part IV/V concept materialization + graph projections | `core/concepts.py`, `search/concept_projection.py`, the five-module concept cluster `search/concept_graph.py` (structure) → `search/concept_tagging.py` / `search/concept_lens.py` → `search/concept_analytics.py` → `search/concept_map.py` |
| Live concept cadence (re-tag, consolidation, edges, coverage snapshot) | `engine/concept_cadence.py` |
| Cross-run index, claims + agent reads | `engine/cross_run_index.py`, `engine/claims.py`, `tools/cross_run_tools.py` |
| Portfolio governance + paid steward lifecycle | `engine/concept_registry.py`, `engine/governance_health.py`, `engine/steward_invocation.py`, `engine/concept_steward.py`, `engine/claim_steward.py`, `engine/task_facets.py` |
| Claim/curation projections + typed owner governance HTTP | `serve/routers/cross_run.py` |
| Claims & Curation UI + evidence validation | `ui/src/ClaimsCuration.jsx`, `ui/src/claimsCurationModel.js` |
| Trace span exporter | `core/tracing.py` |
| Search policies | `search/policy.py` |
| Static HTML lineage tree | `events/htmlview.py` |
| Task adapters + loader | `adapters/tasks.py`, `adapters/toytask.py`, `adapters/regression.py`, `adapters/classification.py`, `adapters/timeseries.py`, `adapters/mlebench*.py`, `adapters/repo_task.py` |
| Strategist / Deep-Research / report | `agents/strategist.py`, `agents/deep_research.py`, `serve/report.py` |
