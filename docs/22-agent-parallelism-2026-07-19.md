# Agent parallelism: parallel node building (design + Variant-1 plan)

**Status:** **implemented / historical design record — superseded by
[docs/23 Layer 2](23-hypothesis-card-kanban-2026-07-20.md) (2026-07-20).** Phases 0–3 of §5 shipped;
Phase 4 is partly outstanding (see §5). The canonical settings are `llm_parallel` (build width) and
`eval_parallel` (eval width); `parallel_build`/`max_parallel` survive as resume/config aliases.
Build fan-out is bulk-synchronous: a batch joins at the build barrier before the loop advances; it is
not a continuously replenished pool of independent research threads.
Original status: design / proposal — 2026-07-19.

**Motivation (as of 2026-07-19 — this paragraph is the pre-implementation snapshot, kept verbatim):** on
a multi-GPU box LoopLab runs at ~1/N utilisation. Per-GPU pinning + `max_parallel>1`
(shipped) let concurrent *evals* land on distinct GPUs, but the engine still **builds nodes one at a
time** and sits **idle for the whole ~30–40 min of a training eval**, so the extra GPUs stay empty. This
doc works through *how* to build (research + code) several nodes in parallel, the two failure modes the
naive version hits (duplicated research, hypothesis contention), what the state-of-the-art does, and a
concrete implementation plan for the recommended option (Variant 1).

---

## 1. Verified current architecture (as of 2026-07-19)

> **Historical snapshot — read every present-tense statement below as "on 2026-07-19".** The serial build
> loop this section describes is gone: `llm_parallel > 1` now fans the `creates` batch out across a role
> pool in worker threads (the `parallel_build_batch` span + `anyio.create_task_group()` block in
> `engine/orchestrator.py::run`), with ids reserved serially up front under `_id_lock`. See the §5 status table for what shipped per phase.

The run spine (`engine/orchestrator.py::run`) each iteration:

1. asks the policy for a batch of `actions` and splits them into `creates` (draft/improve/debug/merge)
   and `evals` (`orchestrator.py:996-998`);
2. if there are `creates`, **builds them all SEQUENTIALLY**:
   ```python
   for a in creates:
       self._create_node(a)          # orchestrator.py:1013-1021 — "sequential -> deterministic ids"
   continue
   ```
3. otherwise dispatches `evals` — **these ARE parallel** up to `max_parallel`, with **CONTINUOUS
   dispatch**: `_dispatch_evals` keeps a `Semaphore(max_parallel)` pool FULL, so the instant a short eval
   frees its slot (and its GPU) the next queued eval is admitted from the SAME batch — no head-of-line
   idle while a long sibling runs (`_dispatch_evals` → `Semaphore(max_parallel)` → `_evaluate`, each eval
   given a no-op `CapacityLimiter(1)`). Each concurrent eval is pinned to a distinct GPU
   (`_evaluate::_acquire_gpu` + `orchestrator._detect_gpu_ids`/`_free_gpus`).

   **GPU-pin transparency (2026-07-20 follow-up).** The pin sets a single-index
   `CUDA_VISIBLE_DEVICES`, so the eval subprocess sees exactly ONE GPU. A command that hardcodes a
   MULTI-device request (`--gpus 2`, `--gpus 0,1`, `--devices 2`, DDP) then crashes in
   pytorch-lightning `pick_multiple_gpus` — this is what failed EVERY node of `rubertlite-dr-unified-v4`
   (the repo README + the PROTECTED eval command both used `--gpus 2`; the repair loop even fixed the
   train stage then reverted it while chasing a later error). Two universal fixes close this:
   - **cap at launch** — `command_eval.cap_gpu_flags` reconciles any recognized device flag to a single
     device inside `run_command_eval::_bound` (the one funnel every stage + the protected score command
     flow through), gated on a single-index CVD in `env`. It caps by SEMANTICS: a COUNT flag (`--gpus 2`
     → `1`), an INDEX flag (`--gpu 3` → `0`, since only ordinal 0 is visible), and device lists/ranges
     (`0,1` / `0-3` → `0`). This makes the pin's "transparent
     remap to cuda:0" promise TRUE even for the protected command the agent cannot edit. No-op when
     unpinned (a serial run legitimately has the whole box).
   - **surface the constraint** — a GPU-pinning cue in `proposal_cues::_set_complexity_hint` (Researcher
     side) + its twin in `crash_repair::_repair_error_context` (repair side) tell every role "you are
     pinned to ONE GPU; use `--gpus 1`", so the FIRST draft and every repair size to a single device and
     the fix STICKS across attempts.

`_create_node` (`orchestrator.py:1680`) is the expensive part and is **fully synchronous**:

- `node_id = max(state.nodes, default=-1) + 1` — id derived from `fold(store.read_all())` **at build
  time** (`orchestrator.py:1682`). Two parallel builds reading the same fold would allocate the **same
  id** → this is *the* reason builds are serialised today.
- append `node_building` (announce);
- `idea = self.researcher.propose(state, parent)` — **one `Idea` per call** (`roles.py:158/268/415`,
  `agent.py:219`, `unified_agent.py:93` all `-> Idea`);
- novelty gate (`_apply_novelty_gate`, one informed re-propose on a semantic dup);
- `code = self.developer.implement(...)` — the ~30–40 min drafting/coding session;
- `_emit_node_created(...)` (appends `node_created`).

The **seed policy already emits a batch**: `GreedyTree.next_actions` returns
`[{"kind": "draft"} for _ in range(n_seeds)]` when `total < n_seeds` (`search/policy.py:239-241`). So the
batch of independent seeds *exists*; only the build loop over it is serial.

**Fold invariants that any parallel build MUST preserve** (`CLAUDE.md` / `events/`):

- **Sole writer** — only the engine main task appends domain events; `self._write_lock` (an
  `anyio.Lock`, `orchestrator.py:490`) serialises appends. (Background/concurrent-research tasks may append
  only the `BACKGROUND_APPENDABLE` selection-neutral types.)
- **Deterministic, monotonic ids** — `node_id` must be unique and reproducible on replay.
- **Exactly one terminal per node**; **fold is order-tolerant** (unknown/extra fields ignored, replay
  recomputes state byte-identically).

**Net:** research (`propose`) + coding (`implement`) for each node is the parallelisable work; the only
hard constraint is that **id allocation and event appends stay serialised/atomic**.

---

## 2. What the state of the art does

- **Anthropic "orchestrator-workers"** (multi-agent research system): a lead agent decomposes the task
  and spawns 3–5 subagents **with clear, disjoint mandates**, each with its own context/tools, running in
  parallel; results are synthesised. Reported −90 % wall-clock on complex tasks. Key lesson: **explicit
  decomposition removes duplication and the disjoint mandates remove contention** — the coordination is
  done once, up front, by the lead. [anthropic.com/engineering/multi-agent-research-system]
- **AlphaEvolve** (DeepMind): a fully **async pipeline** (`asyncio`) of a *controller* + N *LLM samplers*
  + an *evaluation cluster*, over a shared *program database* (MAP-elites / island model). Sampling and
  evaluation overlap continuously; evaluation is "embarrassingly parallel". Key lesson: **decouple
  sample/build from evaluate** and keep a shared population so parents are drawn without re-doing work.
  [arXiv 2506.13131]

Mapping: LoopLab's event log *is* the shared "program database"; the Researcher/Developer split *is* a
built-in planner/executor seam; the seed batch *is* a natural fan-out point. The user's instinct — one
shared researcher emitting N hypotheses, N developers building them — **is exactly orchestrator-workers**
and is the cleanest fit.

---

## 3. The variants

### Variant 1 — Batch-research → parallel build (RECOMMENDED for seed/explore)

One planner pass produces **N distinct `Idea`s** (diverse by construction); N developers `implement` them
**in parallel**; then the N evals run in parallel (already supported, now GPU-pinned).

- **Pros:** research done once (no duplication); ideas disjoint by construction (no contention — matches
  the user's two concerns directly); fills both idle windows (build *and* eval); maps to SOTA
  orchestrator-workers; reuses LoopLab's Researcher/Developer roles.
- **Cons / work:** (a) batch proposal (`propose_batch`) with enforced diversity; (b) parallelise the
  serial `for a in creates` build loop; (c) **atomic id reservation** so parallel builds don't collide;
  (d) keep replay deterministic.
- **Best for:** the **seed phase** (nodes independent) and any **explore** batch. Improve/merge nodes that
  depend on a specific parent's *result* are NOT good batch candidates (see Variant 2).

### Variant 2 — Speculative build-ahead (RECOMMENDED for iterate)

While node N evals (30–40 min), speculatively `propose`+`implement` node N+1 as an **explore** move
(blind to N's metric). When N's eval lands, N+1 is build-ready.

- **Pros:** minimal change; fills the eval-idle window even in the iterate phase; no batch-diversity
  machinery.
- **Cons:** N+1 is proposed *blind* to N's result → wasted if N's outcome would have redirected. Mitigate
  by speculating **only explore moves** (new theme/axis, independent of the current metric), and by
  discarding a speculative node the Strategist deems superseded (a `node_reset` already exists).
- **Best for:** the **iterate phase** where the next best action depends on the just-finished eval.

### Variant 3 — Full async pipeline (AlphaEvolve-style; ENDGAME, not now)

Controller continuously samples parents from the log, N samplers generate children async, evals stream on
the GPU pool — every stage overlaps.

- **Pros:** maximal throughput; SOTA for evolutionary code search.
- **Cons:** largest rewrite; hardest to keep the **replay-deterministic fold** invariant (AlphaEvolve has
  no such contract). Defer until Variants 1–2 are proven insufficient.

### Handling contention / duplication (the user's core worry)

- **Shared planner (Variant 1)** removes it *by construction*: one research pass, explicit assignment.
- Alternative for *independent* researchers: a **work-stealing hypothesis board** — LoopLab already has an
  open-hypothesis board + coverage/novelty signals; N workers atomically *claim* a hypothesis (and avoid
  covered themes). More emergent, but more moving parts than a shared planner. Prefer the planner.

---

## 4. Recommendation

**Variant 1 for seed/explore + Variant 2 for iterate.** Variant 3 is the endgame. Start with Variant 1
because (a) the seed batch and Researcher/Developer split already exist, (b) it directly answers the two
concerns, (c) the only genuinely hard piece — atomic id reservation under replay — is small and local.

---

## 5. Variant 1 — implementation plan

> **Per-phase status (verified against HEAD, 2026-08-04).**
>
> | Phase | Status | Notes |
> |---|---|---|
> | 0 — atomic id reservation | **shipped** | ids reserved serially under `_id_lock` before the fan-out |
> | 1 — parallel build of a `creates` batch | **shipped** | the `parallel_build_batch` task group in `engine/orchestrator.py::run`; per-build `(researcher, developer)` pair from the role pool, as the Risks section below required |
> | 2 — batch proposal with enforced diversity | **shipped** | batch novelty gate present (`_pending_batch_novelty_gated`) |
> | 3 — settings, governance, autonomy | **shipped** | canonical `llm_parallel`/`eval_parallel`; `parallel_build`/`max_parallel` are aliases |
> | 4 — verification | **partial** | the serial golden replay exists (`tests/test_golden_replay.py`); the **"new golden for a 2-wide parallel-build run"** this phase specifies was never added — no golden pins id monotonicity / one-terminal-per-node / deterministic replay under a 2-wide fan-out. The cost guardrail DID ship (the `parallel_build_batch` tracer span). |
>
> **Shape correction:** what shipped is a **bulk-synchronous build barrier**, not the continuous,
> adaptive fan-out §1's contrast with eval dispatch implies. The source says so itself, in the `CODEX AGENT:` comment immediately above the
> `parallel_build_batch` span in `engine/orchestrator.py::run`: "this join is a bulk-synchronous build barrier, not independent
> adaptive research threads. Fast workers cannot select/propose from completed sibling evidence until the
> slowest build and later eval batch finish." That is Variant 3 territory (§3, "ENDGAME, not now"), and
> [02 §12](02-architecture.md) carries the same open annotation. Eval dispatch remains genuinely
> continuous (`Semaphore(max_parallel)`), as §1 describes.

> **Status update (2026-08-14).** Phase 4's missing golden is STILL not added:
> `tests/test_golden_replay.py` has no parallel arm, and no test runs an engine at `llm_parallel=2`
> and pins its replay (`tests/test_multi_build.py` covers the `st.buildings` fold handlers from
> hand-built logs, not a 2-wide run's own log). Cheapest honest construction:
> `tests/factories.py::make_engine(run_dir, llm_parallel=2, …)` with scripted researcher/developer
> stubs over the toy task, run to completion, then (a) assert the folded state satisfies id
> monotonicity and one-terminal-per-node, (b) `fold(store.read_all())` twice and through a second
> `EventStore` open to pin replay determinism, and (c) run a SECOND engine over a fresh dir with the
> same scripted roles and assert the two FOLDED states are equal while the logs' byte order is
> allowed to differ — that last comparison is the property the fan-out actually promises (fold
> order-tolerance across independent nodes; CLAUDE.md invariant 1's build-fan-out seam), and a
> checked-in golden LOG would over-pin the deliberately nondeterministic byte order.

Phased so each step ships behind a flag, keeps the default (serial) path byte-identical, and preserves
every fold invariant. Default OFF → `parallel_build=1` (or `0`=auto by GPU) opts in.

### Phase 0 — atomic id reservation (unblocks everything, no behaviour change)

The single correctness prerequisite. Today `_create_node` reads `max(state.nodes)+1` mid-build; parallel
builds race. Fix:

- Add `_reserve_node_id()`: **under `self._write_lock`**, `fold(store.read_all())`, compute
  `node_id = max(nodes)+1`, append `node_building` for it, return the id. Because the append is inside the
  lock, two concurrent reservations get distinct, monotonic ids — deterministic on replay (ids follow the
  `node_building` append order in the log).
- Refactor `_create_node(action)` → `_create_node(action, node_id=None)`: when `node_id` is provided, skip
  the internal id computation + the `node_building` append (already done by the reservation); otherwise
  behave exactly as today (serial path unchanged).
- **Test:** two reservations under a real store yield `{k, k+1}`; replay of the produced log reconstructs
  the same ids; the serial path is byte-identical (golden replay).

### Phase 1 — parallel build of an existing `creates` batch (mechanism)

Replace the serial loop **only when `parallel_build>1`**:

```python
if creates and self._parallel_build > 1:
    ids = [self._reserve_node_id(a) for a in creates][: self._parallel_build]   # reserve under lock, cheap
    async with anyio.create_task_group() as tg:
        for a, nid in zip(creates, ids):
            tg.start_soon(anyio.to_thread.run_sync, self._create_node, a, nid)   # propose+implement in threads
else:
    for a in creates:                                                            # unchanged serial path
        self._create_node(a)
```

- `propose` + `implement` run off-thread in parallel; each `node_created` append still goes through
  `self._write_lock` (sole-writer preserved). The developer instance must be **safe to call concurrently**
  — either N developer instances or a thread-safe pool (mirror `evaluate.py:290`'s note that a shared
  developer under `max_parallel>1` must be concurrency-safe). Add a `developer_pool` if the CLI/agent
  backend isn't reentrant.
- Cap the fan-out at `parallel_build` (and never exceed the GPU count for the *eval* that follows).
- **Idempotent resume:** a crash mid-batch leaves `node_building` markers without `node_created`; the fold
  already treats those as *building* (not `st.nodes`), and resume rebuilds them — same as today, just N of
  them. Verify the reserved-but-unbuilt case resumes cleanly.

### Phase 2 — batch proposal with enforced diversity (the "one shared researcher → N hypotheses")

Give the Researcher a batch entrypoint so the N ideas are **distinct by construction**, not N independent
rolls that might collide:

- `propose_batch(state, n) -> list[Idea]`: one LLM call that must return **N ideas on N different
  axes/themes** (reuse the concept/coverage map + `find_concept_slugs` to name the axes it must spread
  across); fall back to N sequential `propose` calls with a "avoid these already-chosen directions" hint
  if a backend can't batch. Run the **novelty gate across the batch** (dedup within the batch, not just vs
  history).
- Wire it into the seed phase: when the policy emits a `draft` batch and `parallel_build>1`, call
  `propose_batch(state, len(creates))` once, then Phase-1 the implements. Improve/merge stay per-node
  (they need their parent's result).
- **Test:** `propose_batch(state, 3)` returns 3 ideas on ≥3 distinct axes; the batch novelty gate drops an
  intra-batch duplicate; a non-batching backend degrades to the sequential-with-avoidance path.

### Phase 3 — settings, governance, autonomy

- `Settings.parallel_build: int = 1` (`ge=0`; launch-time `0`=AUTO = the resolved eval width).
  Document in `configuration.md` + the process diagram (docs-in-sync rule).
- The implemented Strategist surface uses canonical `llm_parallel`/`eval_parallel`; legacy
  `parallel_build`/`max_parallel` remain resume/config aliases. Live `0` settles to serial `1` rather
  than re-running launch-time AUTO discovery.
- Keep the producer and eval widths independently bounded. They intentionally need not be equal: LLM
  work and GPU evaluations consume different resources; startup AUTO is only a convenient initial split.

### Phase 4 — verification

- Golden-replay unchanged on the serial path; a new golden for a 2-wide parallel-build run (ids monotonic,
  one terminal per node, deterministic replay).
- Live/smoke: a toy run with `parallel_build=2` builds 2 seeds concurrently, both evals pinned to
  different GPUs, both reach `node_evaluated`.
- Cost guardrail: `log()` the fan-out; never let a batch exceed `parallel_build`.

### Risks / open questions

- **Role reentrancy is CONFIRMED, and is the crux of Phase 1** (verified 2026-07-19, so the plan above is
  now grounded, not speculative):
  - `CliAgentDeveloper`/`LLMDeveloper`/`ValidatingDeveloper` keep the shipped attempt as **instance state**
    (`self.last_files` / `self.last_deleted`, e.g. `cli_agent.py:249`), read by `_create_node` right after
    `implement`. N parallel `implement()` on ONE developer clobber each other's `last_files`.
  - The Researcher is stateful too: `_set_complexity_hint` (proposal_cues.py, 3 sites) and
    `_apply_novelty_gate` (novelty.py:242,252) do `setattr(self.researcher, "_complexity_hint"/
    "_novelty_feedback", …)`, read by the very next `propose`. Parallel builds race on those.
  - So Phase 1 needs a **per-build (researcher, developer) pair**, and `_create_node` + `_set_complexity_hint`
    + `_apply_novelty_gate` must take the roles explicitly (bounded: ~5 sites on the draft path).
  - Building the pool is NOT just `task.build_roles()` — that returns RAW roles missing the `make_roles`
    wiring (the `ValidatingDeveloper` retry/patch-gate wrapper). A `copy.copy` of the wired developer does
    NOT isolate a `WrapsDeveloper`/`ValidatingDeveloper` (it shares `_wrapped`, whose `last_files` still
    races). Options: (a) thread a `role_factory` (from `make_roles`) into the Engine as an OPTIONAL ctor
    arg (default None → pool unavailable → parallel_build clamps to 1, backward-compatible); (b) accept RAW
    pool roles for pool nodes (correct, but pool nodes skip validation-retry — a quality, not correctness,
    gap). Recommend (a).
  - **Conclusion:** Phase 1 is bounded but a real engine-core refactor; implement it as a focused,
    separately-reviewed change (default `parallel_build=1` OFF), NOT bundled with Phase 0. Phase 0 (id
    reservation) already shipped standalone and is fold-identical.
- **Diversity quality** — a weak `propose_batch` could still emit near-dups; the batch novelty gate is the
  backstop, but measure it.
- **Determinism** — id order now follows `node_building` append order under the lock; confirm a re-run
  with the same seed reproduces the same ids (the reservation append order is the tie-break).
- **Interaction with the new `train_monitor` (c05d7b8)** and speculative Variant 2 — keep the seams
  independent.

---

## 6. Relationship to shipped work (2026-07-19)

Already landed and directly supporting this: per-GPU pinning + `max_parallel=0`=AUTO (the eval side of the
parallelism); the STALL watchdog + metric salvage (so a hung parallel eval can't wedge the pool); the
per-experiment TIME-BUDGET cue (so each parallel node is sized to fit). Variant 1 adds the *build* side.

Sources: Anthropic — multi-agent research system
(https://www.anthropic.com/engineering/multi-agent-research-system); AlphaEvolve, arXiv 2506.13131
(https://ar5iv.labs.arxiv.org/html/2506.13131).

---

## 6. Measured 2026-09-03: the second lane starves, and the reason is the BUILD/TRAIN RATIO

The 2026-07-19 motivation above predicted this shape ("builds nodes one at a time and sits idle for
the whole ~30–40 min of a training eval"). Four runs later it is measurable, and the number that
moved is not the width — it is how much of a train a build now occupies.

Lane occupancy, integrating `node_eval_started` against `node_evaluated`/`node_failed` over each
run's own wall clock:

    run                        length    0 evals          1 eval           2 evals
    e5small-dr-unified-v4      100.8h    13.5h (13.4%)    67.4h (66.8%)    19.9h (19.8%)
    e5small-dr-unified-v11      45.4h     3.2h ( 7.0%)    19.0h (41.8%)    23.2h (51.2%)
    e5small-dr-unified-v12      57.9h    17.2h (29.7%)    30.2h (52.1%)    10.5h (18.2%)

v12 leaves BOTH H200s idle for 17.2 hours — four times v11's share — while configured at
`eval_parallel=2`.

Why, in one table:

    run                        median train    median build    build / train
    e5small-dr-unified-v4        445.9 min        39.7 min          8.9%
    e5small-dr-unified-v11       333.6 min        45.8 min         13.7%
    e5small-dr-unified-v12       186.2 min        51.2 min         27.5%

Trains got 58% shorter while builds got 29% longer. A build at 8.9% of a train is invisible — the
next card is ready long before the lane frees. At 27.5% it is not, and the box waits:

    v4/v11   |=== train ===============================|=== train ==============|
             |build|                                    |build|
             lane 2 refilled before it drains

    v12      |=== train =========|=== train =========|      |=== train ====|
             |  build   |        |  build   |   IDLE  |     |  build  | IDLE
             lane 2 drains while the next card is still being made

THE SPECULATIVE LANE IS OFF, AND IT IS OFF ON ALL THREE RUNS.
`engine/speculation.py::_speculation_enabled` requires `speculation_depth > 0` AND a gate-receipt
digest, and `cli/run_cmds.py` restricts that receipt to "ONLY the offline toy benchmark the
calibrated lane is measured on". So Variant 2 (§3) has never run on a real task. That is a
constant across v4/v11/v12 and therefore cannot explain the regression — it explains why nothing
absorbs it.

Two further measurements bound the fix:

* **There is never inventory to build ahead FROM.** Peak cards added-but-not-built is **1** on both
  v11 and v12; v12 has none at all for 41.2h (71.6% of the run). Nine of its nineteen builds start
  with an empty box, and at every one of those starts the number of other unbuilt cards is zero.
* **The board is kept that empty by the admission cap.** Of 413 directions proposed by v12's 36
  research memos, 394 were CAPPED, 2 restated, 0 repeated, 17 admitted — and even those 17 arrive
  singly (`belief_admission` now records this per memo).

So `eval_parallel=2` is a width this pipeline cannot honour while a build is a quarter of a train
and nothing builds ahead. The levers are Variant 2 for real runs, the propose cost itself, or
accepting one lane and not paying for two.
