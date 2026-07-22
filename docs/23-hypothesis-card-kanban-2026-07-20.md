# Hypothesis-Card Kanban re-architecture — design and implementation ledger (2026-07-20)

Status: **Layers 1–6 are implemented and validated in implementation commit `8d9952a1`, which is
pushed to `master`. Card-driven selection remains a default-off, run-start-pinned opt-in. Positive-
depth speculation is admitted only by the current scope-bound receipt for the exact measured Greedy /
quadratic-Toy / depth-1 / max-nodes-12 runtime; every broader workload, policy, depth and budget remains
default-off and fail-closed.**
Grounded in four exhaustive code maps (idea-pipeline ·
strategist/policy · execution/GPU · replay/UI) and an 18-agent per-layer mega-review consolidated in
**§12 — the historical target contract**. The ledger below is the validated current implementation
truth and wins wherever the original target text differs from shipped behavior.

## 0.0.1 Current implementation truth (2026-07-22)

| Area | Landed now | Remaining |
|---|---|---|
| Direction board | `Card` is the bounded lifecycle Kanban; empty/pre-Card runs retain the `Hypothesis` research-direction workflow as a graceful fallback | none |
| Card read model | native monotonic Card mint/link under a process-local `_id_lock`; ownership receipts; exact `node_building.card_id`; lifecycle-aware draft/rerun/inject/ablation writers; bounded per-event input and public projection; durable request/done queue | Card mint and build claim are consecutive appends, not one cross-process atomic/CAS transaction; an exact retry repairs the crash prefix. The folded `cards_enriched` replay journal is not yet capped |
| Enrichment | structured steering snapshots; memo/claim/lesson refs; Researcher proposal + Developer-finalized footprint; final operator edit/priority/resource overlays; schema seams for novelty/cross-run/concept projection | proposal-time selectable Cards do not yet carry trusted novelty/coverage memberships, so those ranking terms are zero until a linked Node supplies later evidence |
| Identity / readiness | receipt-bound `Card.identity`, bounded provenance/blockers, fail-closed `selection_ready`; dropped/merged/gated/superseded work is excluded; exact existing-Card claim and counterfactual speculative freshness are tail/generation fenced | none |
| Concurrency / resources | canonical `eval_parallel`/`llm_parallel`, closed per-lane Strategist allocation (explicit `{}` clears caps), shared broker, memory-aware GPU pool, lifecycle reservations, Docker enforcement for known positive pins and explicit CPU isolation, confirm admission, isolated Card producer/consumer | positive-GPU discovery failure can still launch an unspecified request without a pin; `required-but-unavailable` is not represented separately |
| Selection / UX | default-off Card selector with exact forced prefix, policy-faithful lanes, durable exact ASHA receipts, bounded public lifecycle projection, and optimistic edit/priority/resource/drop controls | broader rollout scopes remain deferred/default-off; `coded` and optional speculative display lanes are reserved rather than derivable from current replay events; browser optimistic state is not yet scoped by run id |

### Stage progress ledger (validated implementation, 2026-07-22)

`Complete` means the acceptance behavior is present in `8d9952a1` and covered by the current validation
and hardware receipts below. Deferred broader rollout scopes are not unfinished work in Layers 1–6.

| Stage | Honest status | Landed commits / evidence | Final validation | Remaining |
|---|---|---|---|---|
| **1a-extract** | **Complete** | `024358f`; retained by `8d9952a1` | current receipt | none |
| **1a-model** | **Complete** | `d4dc621f`, `e66b728c`, `9b1cdb8f`, `bb176cb9`, `8d9952a1` | current receipt | none |
| **1b** | **Complete** | `d10bbe11`, `9cff2fbd`, `b90babd4`, `bb176cb9`, `8d9952a1` | current receipt | none |
| **1c** | **Complete** | `015b699f`, `54ab8d60`, `fb0b438b`, `bb176cb9`, `8d9952a1` | current receipt | none |
| **2** | **Complete** | `15df954b`, `5e871dc5`, `d9940342`, `8d9952a1` | current receipt | none |
| **4** | **Complete** | `547b8d0f`, `bb176cb9`, `8d9952a1` | current receipt | none |
| **3** | **Complete** | `212b1a64`, `4a3bf96b`, `bb176cb9`, `8d9952a1` | 15/15 scorer cases + current receipt | none |
| **5a** | **Complete** | `cc3a666e`, `bb176cb9`, `8d9952a1` | current receipt | none |
| **5b** | **Complete; exact scope admitted** | `bb176cb9`, `8d9952a1`; fail-closed runtime + receipt gate | 3/3 real-GPU A/B pairs | broader scopes remain intentionally unadmitted |
| **6** | **Complete** | `0ff29fed`, `bb176cb9`, `8d9952a1` | current receipt | none |

### Validation and rollout receipt (evidence for commit `8d9952a1`)

> The `implementation` digest below is `speculation_implementation_digest()` over the raw bytes of every
> `looplab/**/*.py`, the required `looplab/serve/settings_ui_schema.json`, and `pyproject.toml` when the
> checkout supplies it, AT commit `8d9952a1`. That hash is intentionally sensitive to any later byte edit —
> including comments, schema help and packaging changes — so this receipt is a point-in-time snapshot of
> that commit, **not** a claim about the current HEAD. A `speculation_depth>0` rollout must regenerate the
> receipt against the exact deployed HEAD (`validated_speculation_gate_receipt` returns the validated
> receipt; `validate_speculation_gate_receipt` provides the boolean check and both fail closed on mismatch).

- Implementation commit: `8d9952a1` (`feat(cards): harden speculative rollout gate`), pushed directly
  to `origin/master` on 2026-07-22.
- Backend: the post-gate full suite exercised 4,996 tests: **4,955 passed / 40 skipped / one pre-existing
  Windows `ChangeTime` timing test flaked**. Independent repetition reproduced that test at 14 pass / 6
  fail; its Windows metadata-token precondition was made deterministic without changing production
  code, after which the complete module passed **33 / 2 skipped**. The composite current result is
  therefore **4,956 passed / 40 skipped / 0 unresolved failures**. The complete speculation-quality
  module separately passed **80/80**.
- UI: **602/602** tests passed. Production Vite build passed with **270 modules transformed**.
- Documentation: strict MkDocs build and `git diff --check` are green for this ledger snapshot.
- Historical local hardware evidence root: `runs/speculation-gate-20260722/`; receipt at validation time:
  `runs/speculation-gate-20260722/speculation-quality.receipt.json` (**24,160 bytes**). `runs/` is ignored
  and this evidence is not shipped in the repository, so these hashes are an audit record rather than a
  reproducible source artifact. The guide now ships the exact seed task inputs and producer commands.
- Effective device: NVIDIA GeForce RTX 5090, UUID
  `GPU-8db535f8-6a6c-d4db-e76b-04b3b9978a10`, PCI `0000:01:00.0`, 32,606 MiB,
  driver `595.79`, CUDA driver version `13020`. Every one of the **64 evaluated nodes** created a real
  CUDA context and allocated/freed exactly 4,096 bytes.
- Evidence totals: **72 physical nodes** across six fresh runs; baselines evaluated 36/36; treatments
  accepted/closed/committed 36/36 exact Card requests, evaluated 28, and recorded 8 zero-cost freshness
  terminals. Scorer fidelity passed **15/15** cases with **0 mismatches**.
- Gate aggregates: mean normalized regret `0.025643226511689373` (limit `0.05`), maximum pair regret
  `0.06747920872708314` (limit `0.10`), mean hit rate `0.7777777777777778` (minimum `0.70`), maximum
  divergence `0.25` (limit `0.34`), minimum trusted coverage ratio `1.0` (minimum `0.90`).

| Seed | Evaluated depth 0 / 1 | Requests closed/committed | Freshness terminals | Normalized regret | Hit rate | Divergence | Coverage ratio |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 12 / 9 | 12 / 12 | 3 | `0.00542002124061201` | `0.75` | `0.25` | `1.0` |
| 1 | 12 / 10 | 12 / 12 | 2 | `0.004030449567372964` | `0.8333333333333334` | `0.16666666666666666` | `1.0` |
| 2 | 12 / 9 | 12 / 12 | 3 | `0.06747920872708314` | `0.75` | `0.25` | `1.0` |

Receipt identity is exact: self digest
`sha256:81bd99000ac63a8422429042ecc34384be048a0d96371e210c7b2d9df43d61e9`, implementation
`sha256:2515754ee4f2996dbe74b956a413b22dfc30e9b819db3bf799df95974114f492`, environment
`sha256:dcab88f14c781a4aa604ea0c3c0e06381744be61a211069eee62065dabf31739`, task profile
`sha256:06029a36f753322c3bff70160938b67c1bd687795989eb98c3c598798d6f456a`, runtime scope
`sha256:b6bbc33fd3538d53ee20dbf5e7c02fba8b15f22f2a5e77188e5bf77c9bd91fb8`, and calibration profile
`sha256:ec3a5b2b925a8019e814d795122f272dc2146a3ae0f70984a9f0b9c8d69ad47a`.

| Evidence directory | Events bytes | Events SHA-256 | Config SHA-256 | Task SHA-256 |
|---|---:|---|---|---|
| `seed-0-depth-0` | 108,822 | `sha256:37065185af8b22af2c371edb677884da623755c10f1b98c2a6dede030890b008` | `sha256:9ada7c44e23f6d3763929cf39ae612dd3d58e098278f671f4b812e5de126d408` | `sha256:874d7f29db0d6d5d2941db153c2f648d0fefb2c19b173ca6a011cb557cb1c1b6` |
| `seed-0-depth-1` | 111,462 | `sha256:0a79fccdec016f0084e27fbae2376d94a5b61b401321f824836452a4fe3a5990` | `sha256:c8f3cb3f1973909286785d2f5b15e01e8a37c59c564bb2171bab0c67609c804d` | `sha256:874d7f29db0d6d5d2941db153c2f648d0fefb2c19b173ca6a011cb557cb1c1b6` |
| `seed-1-depth-0` | 108,446 | `sha256:6706e5bb89eb3dead88cee9c473333c5639c57030582fdbaab58f2d27d1fede3` | `sha256:9ada7c44e23f6d3763929cf39ae612dd3d58e098278f671f4b812e5de126d408` | `sha256:e169ce703d85d31095a0c62ca25b822c93fc23b427fa3098b014d8445a4a8e9a` |
| `seed-1-depth-1` | 111,638 | `sha256:456c3d2b0d2d9e12ebb6e80f0db943ed44ea5a691f47416d188cdb0a30fb501e` | `sha256:c8f3cb3f1973909286785d2f5b15e01e8a37c59c564bb2171bab0c67609c804d` | `sha256:e169ce703d85d31095a0c62ca25b822c93fc23b427fa3098b014d8445a4a8e9a` |
| `seed-2-depth-0` | 108,658 | `sha256:a032d55e1a908e9536f35394aeb55398befb5581f2ed609b6d7a363a8d38e0fe` | `sha256:9ada7c44e23f6d3763929cf39ae612dd3d58e098278f671f4b812e5de126d408` | `sha256:0fa712dd8b40a839cd1d7a38520ff45eee6b0e6bdb3f486e91c5c5dd42fe8cd3` |
| `seed-2-depth-1` | 111,375 | `sha256:11d068375c5492e3bd7c9eec155f5602b12cf697be8227e03037d15b6e789931` | `sha256:c8f3cb3f1973909286785d2f5b15e01e8a37c59c564bb2171bab0c67609c804d` | `sha256:0fa712dd8b40a839cd1d7a38520ff45eee6b0e6bdb3f486e91c5c5dd42fe8cd3` |

### Live implementation TODO

- [x] Layer 1 — native Card model, writers, enrichment, lifecycle and replay safety.
- [x] Layer 2 — canonical eval/LLM concurrency and named lanes.
- [x] Layer 3 — Card selection, scoring, policy fidelity and atomic existing-work claim.
- [x] Layer 4 — footprint-aware resource scheduling and GPU lifecycle reservations.
- [x] Layer 5a — concurrent request-driven producer/consumer execution.
- [x] Layer 5b — durable speculation plus counterfactual freshness gate.
- [x] Layer 6 — Card Kanban, four server-stamped controls and operator-wins overlays.
- [x] Implement the exact 15-case scorer-fidelity gate from §12.7.
- [x] Implement the scope-bound search-quality gate and tamper-evident receipt from §12.7.
- [x] Reject raw/folded Card-queue mismatches, unknown events, cloned sources and cloned semantic trajectories.
- [x] Pin the complete source-owned Settings calibration profile and fail closed before resume mutation.
- [x] Require the exact one-finish, attempt-0, non-tombstoned calibration lifecycle and completed finalization.
- [x] Replace the utility-only GPU check with an exact CUDA context/allocation receipt and stable UUID identity.
- [x] Bind public admission/resume to the exact tested max-nodes/runtime envelope and fixed seed set.
- [x] Finish the adversarial fail-closed audit after those three boundaries land.
- [x] Run one consolidated backend/UI/build/strict-MkDocs validation after implementation freezes.
- [x] Run three fresh depth-0/positive-depth pairs on the effective real GPU and issue the receipt.
- [x] Replace the historical receipt above with exact current counts, evidence paths and digests.
- [x] Commit the validated implementation and push it directly to `master` (`8d9952a1`).
- [x] Commit this final validation ledger and push it directly to `master` (`b5e55a43`).

### Post-review hardening receipt

- An explicit `llm_lane_limits: {}` is a durable atomic clear; omission retains the current allocation.
- `max_nodes + budget_extend.add_nodes` is the hard ceiling for every distinct durable Node reservation,
  including tombstoned, gated, failed and freshness-dropped attempts and outstanding speculative requests.
  The Card selector still ranks an effective filtered view, but only `budget_extend` creates new physical
  capacity. Fork, inject, ablation, serial, parallel and speculative admission share this boundary.
- Widened and speculative ASHA paths deduplicate exact rung/survivor receipts against the durable log.
- With a successfully discovered inventory, positive explicit GPU work remains unavailable on a zero-GPU
  host and a saturated unspecified request waits instead of launching unpinned. Docker refuses a known
  positive pin it cannot enforce, while an explicit CPU reservation injects
  `NVIDIA_VISIBLE_DEVICES=void`. Discovery failure is still an unresolved fail-open boundary for an
  unspecified positive request.
- `EventStore.append_many` exposes a complete logical batch or none of it after a torn write. EventStore,
  replay, command observation, scope/report capture, SSE, timeline paging, run caches and maintenance tools
  expand the bounded storage envelope consistently; generic non-event JSONL readers remain format-agnostic.
- Coverage scoring consumes only complete authorized current memberships and canonicalizes Card aliases,
  case and spacing through the same concept-identity projection.

**Partially resolved Layer-3 safety boundary:** the selector now consumes only `selection_ready`, and the
existing-Card writer re-folds and claims the complete selected lane under a process-local `_id_lock`.
The Card mint and `node_building` claim remain separate unconditional appends without a shared tail CAS.
`Card.actionable` remains a compatibility board flag meaning only “not dropped/gated/abandoned”; it
is true for proposed-without-action, running,
evaluated, and superseded work. It is therefore **not evidence of executability**. The selector may
consume only `selection_ready`, which requires all of the following:

1. one unique `card_added` carrying an exact current v2 `ownership_receipt` (or the preserved
   expanded-v1 transition form) bound to the card id, immutable seed statement, concrete action,
   parent anchors, resource declaration and complete score fence. The frozen original v1 receipt
   establishes legacy identity only and deliberately remains non-selectable because it predates the
   timeout and lifecycle fences;
2. one complete concrete action owner with a supported operator/parent shape and matching
   `parent_generations` for every anchored attempt;
3. with an incumbent, `scored_against` and `scored_against_generation` match its current id/attempt;
   without one, `scored_against_empty` explicitly records that complete empty authority;
4. no linked pending, terminal, failed/superseded, missing, or merged work-item owner.

ID spelling is never the discriminator: even an id that resembles a statement hash is native when its
ownership receipt is valid, while an unreceipted `card-*` string is only a synthesized shadow. Concept
membership is independent metadata with its own completeness receipt; absent concept tags do not block
execution. The native writer produces valid receipts and Layer 3 claims the existing Card without
re-proposing or minting a replacement. Legacy statement-hash Cards, unbound
`card_added` rows and node-only `Idea.card_id` rows remain visible for audit and fail closed.
Layer 5 does not reuse the ready-only selection set verbatim for freshness: once claimed, a Card is
intentionally in-flight. The implemented gate uses the counterfactual/include-owned API plus the exact
durable `card_build_done → node_id` link before every scorer consult and again before GPU admission.

## 0. Pre-implementation baseline, motivation and build order

Before Layers 1–6, the spine strictly alternated `build-batch → eval-batch`; in steady state every policy proposed
**one** node per iteration (almost always `state.best()`), so GPUs idle during every propose. The
hypothesis board (`state.hypotheses`, `Hypothesis.priority`) is **derived every fold and never read by
node selection** — it is advice, not a work queue. Resources are hardcoded (1 GPU per eval), which is
why the `--gpus 2` prod bug was unfixable by the loop.

The pleasant surprise (map D): the board is **already** a fold projection (`_derive_hypotheses`),
**already** serialized to the UI, and **already** steerable (`hypothesis_*` + `set_strategy` control
events); `ui/src/panels.jsx::HypothesisBoard` is a shipping kanban. So this is *extending a
load-bearing seam*, not building from scratch.

**Build order — do all of it, in 6 layers by dependency + risk** (each self-standing, with additive,
default-off, or explicit-operator behavior preserving today's no-opt-in path; each through mega-analysis
→ build → mega-review → tests → push; the authoritative dependency reorder is in §12.6):

1. **Data — the card** (this doc). First-class durable card entity + land every homeless signal.
2. **Two pools** — split LLM-concurrency (`llm_parallel`) from GPU-concurrency (`eval_parallel`).
3. **Brain — scoring** — the card queue drives selection; Strategist sets direction, policies score.
4. **Resources** — agent-declared footprint + bin-packing scheduler; fixes `--gpus` by construction.
5. **Execution without idle** — concurrent producer/consumer, speculative pre-build, freshness gate.
6. **Board UX** — extend the kanban to the full lifecycle + card-steering control events.

Owner decisions locked: readiness is a derived read-model (`selection_ready`), never event-authoritative;
footprint = an accreted resource field — **Researcher proposes it, Developer finalizes from code**;
speculation depth = enough to keep GPUs utilized, freshness gate on; **stable `card_id` immediately**.

## 0.5. Historical design-review resolutions (2026-07-20)

Two adversarial design reviews (replay-safety + completeness) found real defects. This section records
the design target that superseded flagged claims in §3–§8 at the time; the current implementation
ledger in §0.0.1 and explicit post-review caveats later in this document supersede this section.

**Split Layer 1 into three shippable increments** (was one over-large layer):

- **1a — structural mirror.** `Card` model + `Idea.card_id` + a `_derive_cards` that ONLY mirrors
  `_derive_hypotheses` (seed from `card_added`; link nodes by `card_id` else statement-hash) + golden
  regen. `st.cards` is a byte-for-byte shadow of `st.hypotheses` where they overlap; old logs fold
  identically. Smallest surface — the risky fold refactor + golden diff land alone. No signal landing
  beyond what hypotheses already carry.
- **1b — additive signal landing.** `card_enriched` + land the non-contentious classes one at a time
  (steering-context snapshot; `cross_run_prior`/novelty verdict as card fields; deep-research memo ref;
  researcher-proposed footprint).
- **1c — dropped/rejected + node-less intra-batch dup.** Isolated behind its own review (identity
  question below).

**Historical target: `card_id` — monotonic, main-task-only, with an atomic mint.** A monotonic
`card-{k}` id CANNOT be background-appendable (a bg mint races on `max+1`; the splice test cannot catch
a write-time id race — it only proves fold-order tolerance of an already-written event). Decision:
engine-minted monotonic, **main-task-only**, reserved+appended **atomically under the shared
`_id_lock`**, ceiling `1 + max(card_added ids in log)` — exactly like node ids. `card_added` is **thin**
(id + statement + source + steering snapshot, all available at proposal) and appended **immediately at
mint**; heavy enrichment defers to `card_enriched`. NEVER mint → slow work → append (TOCTOU). Resume is
deterministic (counter = max-over-log + 1). All Layer-1 card events are main-task-written; background
Layer 5 slow workers never reserve ids or append Card lifecycle events: they return bounded in-memory
proposal/build results and the main task revalidates and commits under the normal lock/CAS boundary.
Here “atomic” covers only reserving and appending the `card_added` mint. The later
`node_building.card_id` claim is a separate append and is not one cross-process atomic/CAS transaction
with the mint; §0.0.1 records that shipped limitation and its exact-retry recovery behavior.

**`card_id` on `Idea` ONLY (not `Node`).** It flows through `node_created` for free
(`durable_idea_payload` + replay `Idea(**d["idea"])`). `_derive_cards` links via `idea.card_id`. Never
add `Node.card_id`; never back-fill it in a post-pass (that is a node mutation).

**`card_id` DEFERS the hash join, does not solve it.** The creation-time "same hypothesis?" lookup
stays by `hypothesis_id` in Layer 1 — a paraphrase still mints a new card. `card_id` only stabilizes
RE-reads (evidence relinks by id once assigned). Researcher emitting an existing id is a later layer.
(Downgrades §4/§7.7 wording.)

**Novelty verdict = ANNOTATION on the running node's card, NOT a `card_dropped` (fixes §7.1).** The
premise "a rejected proposal becomes no node" is false: `_apply_novelty_gate` always yields a node
(repropose / keep-dup / numeric-nudge), and the `novelty_rejected` prospective id is the SAME id that
node receives — a `card_dropped` for it would collide. So the verdict lands as a card field
(`novelty_verdict`), sourced from the already-folded `novelty_events`/`novelty_grades`. The one truly
node-less case (`_intra_batch_dup`, emits no event) is deferred to 1c.

**`_derive_cards` determinism — pinned.** Iterate `sorted(st.nodes)`; extract `_derive_hypotheses`'
evidence/verdict logic as a PURE helper returning values (never stamping onto Node/Hypothesis); reuse
its exact tombstoned/aborted/feasible filters; canonicalize merges cycle-safe (reuse `_canon`) and
apply `card_dropped`/deleted LAST; `card_enriched` last-write is by event **seq** (valid because all
card events are main-task-written). Runs inside `_finalize_fold` AFTER `_select_best` (no selection
leak) on the FoldCursor deep-copy (no suffix leak).

**Footprint (layer split).** Researcher-proposed footprint rides the `Idea` (like `eval_profile`) — in
1b. Developer-finalize needs a new registry-guarded `DEVELOPER_OUTPUT_ATTRS` member and is wired with
its actual USE (bin-packing) in Layer 4 — NOT in Layer 1.

**Layer 1 is durable CAPTURE, not delivery.** Captured signals reach the next proposal / UI only in
Layers 3/6. Expect no steering/behavior change from Layer 1.

**Golden regen is broad.** `model_dump` gains `card_id: null` on every nested `Idea` plus `cards: {}` —
additive but touches every node; state it in the commit.

**Producers to add to the §7 map:** operator `pending_hints`; the numeric E1 nudge (a modified idea
that RUNS — annotate its node's card, not a drop); the surrogate k-NN K→1 pick (discards as homeless as
foresight's). Land or explicitly defer each.

## 1. Invariants Layer 1 must satisfy (the hard rules, from map D)

1. `fold` stays pure/deterministic/order-tolerant — no I/O, no LLM, no wall-clock in any card handler
   or the `_derive_cards` post-pass.
2. Every new `EV_*` lands in **exactly one** bucket — `replay._HANDLERS` (folded) or `DIAGNOSTIC_EVENTS`
   (ignored) — or `tests/test_event_types.py` fails.
3. Sole-writer of folded events; a card event that a background task writes must pass the
   `BACKGROUND_APPENDABLE` splice test (`tests/test_background_appendable.py`).
4. Additive-only schema; old logs fold **byte-identically** (golden-replay gate `tests/test_golden_replay.py`).
5. `FoldCursor.snapshot()` deep-copies before finalize — the `_derive_cards` post-pass must be
   destructive-safe on the copy and never leak into the next suffix.

## 2. Layer 1 scope + non-goals

**Target scope (not a statement that every item has landed):** a first-class, durable, folded **Card** entity with a stable id; a `_derive_cards`
projection modeled on `_derive_hypotheses`; new advisory `card_*` events; nodes carry `card_id`; every
homeless producer output (§7) attached to its card; the card store serialized to the UI (rides the
existing public dump for free).

**Explicit non-goals for Layer 1 (deferred to later layers):** NO change to node selection
(`next_actions` untouched); NO speculative build; NO scheduler/resource change; NO concurrency change.
The card store is still **advisory** — exactly like the hypothesis board today. Default behavior is
byte-identical to today; the card store is pure additive folded state.

## 3. The Card model

The target architecture has two deliberately different identities:

- `Hypothesis` is the research-direction aggregate: it may collect many experiments and verdicts.
- `Card` is exactly one immutable proposal/work item: its native id owns one action receipt and one
  lifecycle. Merging directions must not silently merge executable actions.

The currently landed `RunState.cards: dict[str, Card]` is a **migration read model** derived by
`_derive_cards`. For compatibility it still mirrors hash-joined hypotheses, unions legacy evidence,
and preserves the earliest action when old aliases merge. Those shadow rows are useful audit data but
are not native executable Cards: the identity/readiness guard keeps them `selection_ready=false`.
Layer 3 must remain disabled until the native mint/link lifecycle removes that ambiguity at the writer.

| Card field | Filled by (producer → today's home) | Layer-1 status |
|---|---|---|
| `id` (opaque, stable, wording-independent) | engine-minted at `card_added` (§4) | new |
| `statement`, `rationale`, `source` | `Idea.hypothesis`/`rationale` + `hypothesis_added.source` | reuse |
| `idea` block: `operator, params, space, eval_profile` | `Idea` (`models.py:166-218`) via the built node | link |
| `concept_tags` + exact `concept_source` receipt (`provenance_tier` compatibility scalar) | one deterministic action/evidence owner's folded `node_concepts`; node-less proposal snapshot until linked | link |
| `novelty_verdict` (grade/level, near_node, recommendation) | `novelty_graded`/`novelty_rejected` — **re-home** | new field |
| `cross_run_prior` (matched concepts, prior runs+outcomes) | `cross_run_prior` — **re-home** | new field |
| `foresight_rank` + `confidence` (+ source) | `hypothesis_ranked` + `foresight_selected` | reuse |
| `footprint` (`gpus`, `mem?`, `timeout`; `proposed_by`, `finalized_by`) | Researcher proposes → Developer finalizes | new field (value used only in Layer 4) |
| `node_ids` / `evidence` | nodes whose `idea.card_id == id` | derived |
| `research_origin`, `lesson_refs`, `claim_refs` | `Node.research_origin`, memo, lessons/claims stores | link |
| `steering_context` (why proposed: cues + strategist stance + memo id) | proposal-cue hints + `active_strategy` — **homeless today** | new field |
| `status` (derived maturity/lifecycle) | `_derive_cards` from fields + `st.nodes` | derived |
| `merged_into` / `aliases` | `hypothesis_merged` → `card_merged` | reuse |
| `dropped_reason` | `card_dropped` / novelty-reject / freshness (later) | new field |

`footprint` is stored in Layer 1 but **not consumed** until Layer 4; storing it now keeps the schema
stable and lets the Researcher/Developer start populating it immediately.

## 4. Card identity + migration off the statement hash

**Problem today:** a node links to a hypothesis only if `idea.hypothesis` text hashes
(`hypothesis_id`, `models.py:533-542`) to an existing statement — any paraphrase drift silently fails
to link evidence, so a card can sit "open" forever despite being tested.

**Decision (owner: stable id immediately):**

- `card_id` is an **engine-minted opaque monotonic id** (`card-{k}`, reserved under a lock and
  **recorded in the `card_added` event**), exactly like node ids. It is in the log, so replay reads it
  (never regenerated → deterministic). It is decoupled from statement wording.
- `Idea`/`Node` gain an additive `card_id: Optional[str]` field. A node built for a card carries its
  `card_id` in `node_created` → evidence links by id, not by hash.
- **Back-compat migration (fold-side, no rewrite):** `_derive_cards` links a node to a card by
  `idea.card_id` when present; when absent (legacy logs / pre-Layer-1 nodes) it **falls back to the
  statement-hash join** exactly as `_derive_hypotheses` does today. So old logs fold identically and
  new logs get the robust id join. The hash join is retained as a legacy fallback, not removed.
- The Researcher, reading the board, references an existing card's `card_id` when proposing an idea for
  it; a genuinely new hypothesis mints a new card. (The prompt contract changes minimally in a later
  layer; in Layer 1 the researcher still states text and the engine mints/looks-up the card.)

## 5. New event types + registry classification

> **Superseded by §12.** The "`BACKGROUND_APPENDABLE`-eligible" column below is **retracted**: a monotonic
> `card_id` cannot be background-minted, so **all** Layer-1 card events are main-task-written and NONE go
> in `BACKGROUND_APPENDABLE` (decision 29). The event set is also larger — see §12.3/§12.2-E.

All Layer-1 card events are **advisory** (board = "transient advice; selection is what replay pins",
the license already granted at `types.py:274-283`) — none are selection-affecting, so none touch
`next_actions`/best-selection. Modeled byte-for-byte on `hypothesis_added/updated/ranked/merged`.

| Event | Bucket | Written by | Models on |
|---|---|---|---|
| `card_added` (id, statement, source, footprint-proposal, steering snapshot) | folded, main-task-only | engine main task | `hypothesis_added` |
| `card_enriched` (novelty verdict / cross-run prior / concept tags / footprint-finalize) | folded, advisory | engine | `hypothesis_updated` |
| `card_ranked` (priority order) | folded, advisory | engine (foresight) | `hypothesis_ranked` |
| `card_merged` (alias → canonical) | folded, advisory | engine consolidation | `hypothesis_merged` |
| `card_dropped` (reason) | folded, advisory | engine / operator | `hypothesis_updated`(deleted) |

Each new `EV_*` constant goes in `events/types.py`, added to `replay._HANDLERS` (folded), and — for the
subset written by the concurrent enrichment task — to `BACKGROUND_APPENDABLE` **only after** its
membership is proven by extending `tests/test_background_appendable.py` (splice-position invariance).
`tests/test_event_types.py` guards the one-bucket rule. All Card lifecycle events remain main-task-only.
Layer 5 workers buffer proposal audit intents and computed artifacts; the main task publishes them only
after the proposal's epoch/parent/best/cue fence still matches.

## 6. The `_derive_cards` fold projection

> **Step order superseded by §12 (decision 20).** The internal order must mirror `_derive_hypotheses`
> exactly — **`card_merged` evidence-UNION BEFORE the verdict helper** — otherwise a merged card's verdict
> diverges from its hash-joined hypothesis silently (the toy golden has no merges, so the tripwire stays
> green while the invariant is broken). The steps below list verdict before merge and are retracted on that
> point only.

A new post-pass in `_finalize_fold` (after `_derive_hypotheses`), pure and order-tolerant, that builds
`st.cards`:

1. Seed cards from `card_added` events (authoritative id + statement + source).
2. Fold `card_enriched` deltas onto each card (last-write-wins per field, generation-fenced where the
   value came from a node lifecycle).
3. Link nodes: for each node, `idea.card_id` (new) else statement-hash (legacy) → append to the card's
   `node_ids`; compute evidence-derived fields (`best_delta`, verdict) exactly as `_derive_hypotheses`
   does (`replay.py:2965-3012`) — **reuse that logic**, do not duplicate.
4. Apply `card_merged` aliasing (cycle-safe, deterministic) and `card_dropped` overrides.
5. Stamp `foresight_rank`/`priority` from the latest `card_ranked` (latest-wins).
6. Derive `status` (maturity: has-statement / has-code / footprint-known / ready-gate; running from
   `st.building`/`st.buildings` + pending; done from terminal) — **no stored status**.

The existing `_derive_hypotheses` is refactored so `_derive_cards` reuses its evidence/verdict helpers
rather than re-implementing them; `state.hypotheses` is kept (derived from `st.cards` or in parallel)
until later layers retire it, so nothing that reads the board breaks.

## 7. Homeless-signal landing (lossless mapping — the point of Layer 1)

From map A, these currently evaporate or float in prospective-id lists; Layer 1 gives each a card home:

1. **Rejected-proposal novelty verdicts** — an idea the gate rejects becomes no node today; its
   "already tried #N" verdict lives only in a `novelty_rejected` event keyed by a *prospective* id →
   Layer 1 mints a `card_dropped` card carrying the verdict, so the "already tried" signal survives.
   **Highest-value gap.**
2. **~12 proposal-cue hint strings** (`_set_complexity_hint`/`_stamp_novelty_hint`) → snapshot into the
   card's `steering_context` at `card_added` time.
3. **`cross_run_prior` / `novelty_graded`** (folded to run-level prospective-id lists, read by nothing)
   → attach to the card's `cross_run_prior` / `novelty_verdict`.
4. **Deep-research memo evidence** (`findings/claims/sources` live only in the `research` timeline) →
   card's `research_origin` references the memo id.
5. **Foresight discarded K-1 + calibration** → the pick is on the card; the K-1 rejected ideas may
   become `card_dropped` cards (decided at review — may defer to keep Layer 1 lean).
6. **Lesson-guard / offline analytics** (`taxonomy_dedup`, `novelty_recall`, `research_targeting`,
   currently write nothing) → first-class card annotations (research_targeting is the natural scorer,
   wired in Layer 3).
7. **Fragile hash join** → solved by `card_id` (§4).

## 8. Back-compat + golden-replay safety

- `RunState.cards` is `Field(default_factory=dict)`, but an old log that contains
  `idea.hypothesis`/`hypothesis_added` now derives legacy statement-hash Card shadows. Only old logs
  with no hypothesis/card inputs fold to `{}`. Those compatibility rows have `identity.kind=legacy_hash`
  and `selection_ready=false`; `Idea.card_id` still defaults to `None`.
- No card event is selection-affecting → best-selection, confirm-eligibility, and every
  `pending_nodes()`-keyed guarantee are untouched → `test_options_divergence` / golden stay green.
- The statement-hash fallback keeps every existing hypothesis-board test passing.

## 9. Config knobs in Layer 1

Minimal — the card store itself is unconditional additive folded state (no behavior gate needed since
it changes no behavior). If any card-write is gated (e.g. background enrichment), it follows the
`watchdog_reflection` template: `Settings` field + same-named `EngineOptions` (conservative default) +
`_opt()` wire + settings-UI count bump + config-doc row + diagram edit. Footprint has no knob (it's a
data field). Decide at review whether Layer 1 introduces any knob at all (leaning: none).

## 10. Test plan

- `tests/test_cards.py` (new): fold of `card_*` events → `st.cards`; id stability across paraphrase;
  node↔card link by `card_id` and legacy hash fallback; merged/dropped; derived status; a rejected
  proposal produces a `dropped` card carrying the verdict.
- `tests/test_event_types.py`: new constants in exactly one bucket.
- `tests/test_background_appendable.py`: extend only if a card event is background-written.
- `tests/test_golden_replay.py`: regenerate snapshot (single additive `cards: {}` diff).
- `tests/test_events_replay.py`: `_derive_cards` determinism + order-tolerance + old-log byte-identity.
- Serve: the card store appears in `state_payload` (rides the public dump).
- UI: `buildingModel`-style unit test if any new client projection is added (else deferred to Layer 6).

## 11. Open design questions for the design review

1. **Card store: derived vs stored.** Proposal: **derived** (`_derive_cards`, like the board) for
   replay-purity; the `card_*` events are the durable inputs, `st.cards` the projection. Confirm we do
   not want a stored card table (would need its own idempotence machinery).
2. **Retire `hypotheses` or keep both?** Proposal: keep `state.hypotheses` deriving in parallel through
   Layer 1–2 so no reader breaks; retire once selection + UI read `st.cards` (Layer 3/6).
3. **How much enrichment is main-task vs background in Layer 1.** Proposal: all main-task in Layer 1
   (simplest, no `BACKGROUND_APPENDABLE` proof needed); move enrichment to a bg task in Layer 5.
4. **Do K-1 foresight rejects become dropped cards now?** Proposal: defer to keep Layer 1 lean; the
   acute loss is the *novelty-rejected* verdict (item 7.1), which we do land now.

> **All four questions above are resolved in §12** (the mega-review). Q1 → derived (`_derive_cards`,
> never a stored table); Q2 → keep both boards through L1–L6, retire in a post-L6 deprecation window;
> Q3 → **all Layer-1 card events are main-task-written; NONE are `BACKGROUND_APPENDABLE`** (a monotonic
> `card_id` cannot be background-minted); Q4 → defer K-1 foresight rejects.

## 12. Mega-review consolidation (2026-07-20) — historical target contract

An 18-agent mega-review ran a **design + independent adversarial verifier** for each of eight targets —
the concurrent producer/consumer **core** (spans L2/L3/L5), **L2** two-pools, **L3** card-driven
selection, **L4** declared-footprint bin-packing, **L5** speculative pre-build + freshness gate, **L6**
board UX, a cross-cutting **search-quality** lane, and **L1 residuals** (cost/serialization/refactor).
Every layer came back **sound-with-fixes** (46 confirmed flaws total); a consolidation pass resolved the
cross-layer interlocks and a finalize pass produced the build contract below.

**This section was the authoritative build contract on 2026-07-20.** It still supersedes the earlier
proposal text in §3–§8 and §11, but the current implementation ledger in §0.0.1 and explicit
post-review caveats supersede it where shipped behavior differs. It extends §0.5; it does **not**
loosen the §1 invariants. Highlights that overturned earlier text:
`card_added` is monotonic **main-task-only** (not background-appendable — §5's "BACKGROUND_APPENDABLE-
eligible" wording is retracted); `_derive_cards`' internal order is **merge-union BEFORE verdict** (§6's
step order is retracted); `node_building` must **carry `card_id`** and the card is minted for speculative
drafts too (extends §4); the queue **is the folded log**, never an in-memory `asyncio.Queue`.

### 12.1 The unified execution model (the L2/L3/L5 interlock, resolved)

The three hardest layers form ONE story, and the sole-writer log is what makes it replay-safe:

- **The durable queue IS the log.** Producer output = a folded `node_created` linked by `idea.card_id`.
  Consumer input = `fold(read_all()).cards` / `pending_nodes()`. In-flight build = `st.buildings` (a
  `node_building` without its `node_created`); in-flight eval = pending coded nodes; done = terminal.
  No queue, selection, ownership, or lifecycle decision lives only in memory, so a crash loses no
  durable intent and resume is `fold(read_all())`. Recomputable proposal/code artifacts may live in
  bounded memory buffers and are deliberately regenerated from a durable request or a fresh fenced fold
  after a crash.
- **Sole-writer of the speculative `node_created` is the MAIN task.** The background producer coroutine
  fills a pure-Python buffer `_spec_builds[(card_id, generation)]` and writes **no folded event**
  (optionally a fold-ignored `DIAGNOSTIC EV_CARD_BUILT`). This **overrides** the core sub-design's
  "producer appends via the Variant-1 worker-thread seam": `node_created` is selection-affecting and is
  NOT in `BACKGROUND_APPENDABLE`, and the Variant-1 seam is drafts-only (`all(kind=='draft')`,
  orchestrator.py:1086). Variant-1 stays scoped to the initial all-drafts seed batch. The separate
  steady-state raw-proposal worker similarly returns only `_spec_raw_stage_result`; the main task alone
  revalidates its generation/parents/best/cue/tail fence and appends the staged Card.
- **The build producer is strictly request-driven.** The main task elects the next buildable Card via the
  L3 scorer and appends `card_build_requested{card_id:K, generation:G}` — ONE durable event that is
  simultaneously the selection point AND the compute gate (folded, main-task-only, reserved +
  generation-fenced under `_id_lock`; `st.card_build_requests` is a verbatim mirror of `_on_fork`). The
  producer fills exactly `_spec_builds[(K,G)]` for the head request; it never free-selects "the top card"
  (that would let its choice diverge from the pinned request and wedge the gate forever). When no ready
  Card owns a built-in policy's counterfactual raw create action while an eval is running, an isolated
  Researcher may prepare that proposal without folded writes; the main task stages the Card under the
  full fence first and only then elects its exact durable build request. Unsupported custom-policy and
  ablation lanes, or a rejected/stale proposal, return once to the serial outer boundary.
- **Producer-failure give-up is mandatory.** An async producer sits between request and done (unlike
  fork, whose work is synchronous), so a crash/timeout would wedge the pipeline and re-wedge on every
  resume. The main-task commit branch `_serve_card_builds` (sibling of `_serve_forced_requests`,
  orchestrator.py:1430) is **crash-recovery-first** (a node with `idea.card_id==K` at gen G already
  exists → only append `card_build_done`, never double-create), else reserves the exact
  `node_building` under the short process-local `_id_lock` (without a tail CAS shared with Card mint), commits the ready buffer via the reserved
  `_create_node(precoded=…)` outside that lock, then appends
  `card_build_done{node_id, speculative:true}`; otherwise it
  advances the counter with `card_build_done{skipped:'producer_failed'|'stale'}` — mirroring
  `_serve_forced_requests` always appending `fork_done` even on `skipped:'stale_generation'`.
- **The freshness gate is engine-live, never in fold, and runs BEFORE every scorer consult.** Every
  policy returns EVALUATE for all `pending_nodes()` first and never returns `[]` while a pending node
  exists, so an un-dropped stale speculation would be force-evaluated AND confirm/holdout (which fire
  only on empty actions, orchestrator.py:1008) would never trigger. Each loop turn: **re-fold → drop-
  stale → then consult the scorer.** Drop criterion = the card is **no longer ELIGIBLE** (`card_merged`/
  `card_dropped`, footprint no longer fits, OR not a member of L3's **current selection SET**) — **never
  "strictly rank-1"** (that collapses a population lane to one card and drops N-1 of every N speculative
  builds, re-idling the GPUs the layer exists to fill). A drop reuses the existing
  `node_failed(reason='superseded')` terminal (whitelisted, replay.py:749), honoring one-terminal-per-node.
- **Quiescence is derived from folded state plus bounded worker/result/in-flight latches.** The
  structured session uses scoped task groups; its loops stay alive
  while ANY of {`st.buildings` non-empty, a pending-coded node exists, `card_build_requests` >
  `card_builds_done`, a buffered/in-flight build or raw proposal, the producer has a buildable Card, any
  in-flight eval}; only when all are simultaneously empty do both return. Structured concurrency joins
  every build/eval child before the outer loop can finalize (writers are never `abandon_on_cancel`), so
  no write lands past finalize and the
  **unchanged** confirm/holdout/finalize path (orchestrator.py:1008–1048) runs exactly as today.
- **Everything is behind defaults that reproduce today byte-for-byte:** `card_driven_selection=False`,
  `speculation_depth=0`, `llm_parallel=None(→1)`, `eval_parallel=None(→max_parallel)`, freshness off,
  footprint unspecified. `_spawn_research` overlap (orchestrator.py:1585) is preserved inside the
  structured session.

### 12.2 Finalized decisions (30), grouped

**A — Concurrency, sole-writer, resume:**
1. Durable queue/selection/ownership state = the folded log; only bounded, recomputable worker artifacts
   live in memory (§12.1).
2. Sole-writer of speculative `node_created` and staged `card_added` = main task; workers fill
   `_spec_builds` / `_spec_raw_stage_result` only.
3. Build producer strictly request-driven; `card_build_requested` is the single build-selection+compute
   gate. A raw fallback proposal is worker-prepared but must be main-task-staged as a fenced Card before
   that exact Card can receive a request.
4. Producer-failure give-up branch (`card_build_done{skipped}`) is mandatory; crash-recovery-first.
5. Producer uses an **isolated** `(researcher, developer)` pair from the L2 `llm_parallel` role pool,
   never `self.developer` (else it cross-wires `last_files`/`last_deleted` with main-task builds/repairs,
   the race at evaluate.py:362–367).
6. Quiescence is derived from folded state plus bounded worker/result/in-flight latches; outer finalize
   path unchanged.
7. `_spawn_research` overlap is preserved inside the structured session.

**B — Freshness & scoring:**
8. Freshness drop = "no longer ELIGIBLE", never "rank-1"; consults L3's current selection SET.
9. Freshness gate runs before every scorer consult (re-fold → drop → consult), not only pre-GPU.
10. L3 scorer preserves the policy forced-prefix in **exact** order: (1) `pending_nodes()`→evaluate-all,
    (2) `debug_action` first-failed-leaf-within-depth as a FORCED step, (3) `len(nodes)>=max_nodes`→`[]`,
    (4) seed phase, (5) card scoring. `card_next_actions` **never** returns a non-forced `[]` while budget
    remains — it falls back to `policy.next_actions(state)`.
11. Greedy vs population = the `card_select` step. Greedy → top-1 (byte-identical intent in the single-
    hot-card case, where `anchor==state.best()`). Population (evolutionary/mcts/asha) → a diversified
    top-K lane; **mcts/asha are a documented, flag-gated intended behavior change** (UCB/rung selection
    does not reduce to a best-metric anchor) — or the improve target is chosen at node granularity within
    the lane. Open-band cards score by a lexicographic `(band, exploration_key)` tuple.
12. **ASHA speculation capped to depth-0 across rung boundaries** (successive-halving computes survivors
    from evaluated members only; the blanket "population→wide speculation" rule holds for
    evolutionary/mcts but not ASHA). Merge-card freshness: fresh iff BOTH parents remain in
    `rank_by_metric[:2]` AND breedable.
13. **The effective Card ranking denominator excludes tombstoned/gated/freshness-dropped nodes, while
    the physical reservation ceiling never refunds them.** Tombstoned attempts remain durable evidence
    in `st.nodes`; failed `node_building` ids and unmaterialized speculative requests are reservations too.
    The engine therefore translates the remaining raw slots into the filtered policy view, but every
    distinct reservation still consumes `max_nodes + budget_extend.add_nodes`. This preserves selection
    quality inside the capacity the operator actually granted without allowing repeated drop/retry cycles
    to bypass the hard limit. **Landed across L3/L5 and shared by every Node-creation path.**
14. Developer-crash sentinel eval race closed structurally: the consumer readiness predicate **excludes**
    nodes whose code is the `'(developer error:'` sentinel (a pure fold-readable `node.code`/`status`
    check); never rely on `node_failed`-wins timing.

**C — Resources & GPU admission (universal, never hardcoded):**
15. `eval_parallel` and GPU inventory are **orthogonal axes.** `eval_parallel` bounds concurrent evals
    (the `_dispatch_evals` semaphore) and is the sole ceiling for `gpus=0` CPU-only cards; L4 bin-packs
    the declared footprint against the GPU **inventory** `len(_gpu_ids)`(+mem). Packing footprint against
    `eval_parallel` would re-seed the `--gpus 2` mis-counted-resource bug.
16. **UNSPECIFIED footprint reproduces today's per-branch behavior exactly** (serial unpinned/whole-box —
    needed for DataParallel/DDP; parallel one-per-eval). Distinguish UNSPECIFIED (`None`/`gpus None`) from
    an explicit `gpus=1`; only an explicit declaration pins/packs.
17. **Over-declaration is clamped, never allowed to wedge.** `_acquire_gpus` is all-or-nothing, so a
    hallucinated `gpus>len(_gpu_ids)` can never satisfy and hangs the task group forever. Clamp
    `need=max(0, min(need, len(_gpu_ids)))` on a non-empty pool + a proposal/repair cue; keep empty-pool
    →`[]`. In L6 clamp operator `card_resource_pinned` to the feasible envelope (reject out-of-range with
    HTTP 400; re-clamp on resume against the possibly-shrunk envelope).
18. GPU-admission primitive is a **race-free** wait (Semaphore + re-check-after-acquire, OR an
    `anyio.Condition` with `notify_all` under lock) — the hand-rolled "re-created + set in `finally`"
    `anyio.Event` is a lost-wakeup deadlock and is **rejected**.
19. **CPU-only (`gpus=0`) cards must not head-of-line-block** behind a stalled large-GPU card: admit by
    **pop-first-fitting** (scan for the first eval whose footprint currently fits).

**D — Card schema & the fold projection:**
20. `_derive_cards` internal order mirrors `_derive_hypotheses` **exactly**: (1) seed + node-link (id
    join else immutable-seed hash fallback), (2) `card_merged` alias/`_canon` evidence **UNION**, (3)
    run-global record-setter set once, (4) the **shared extracted verdict helper** on the merged set, (5)
    `card_dropped`/deleted overrides, (6) `card_ranked` priority, (7) operator maps overlaid in a FIXED
    LAST phase, (8) derive status. (§6's verdict-before-merge order is retracted — it breaks the 1a shadow
    invariant whenever a `card_merged` exists, silently, because the toy golden has no merges.)
21. **The verdict-helper extraction is a SEPARATE first commit** (`test_golden_replay` green *without*
    regen, zero hypotheses-subtree diff = the byte-identity tripwire); cards land in a second commit with
    the single additive golden regen. Never squash — a squash would mask a non-pure extraction.
22. `st.cards` is assigned ONLY inside `_derive_cards` (like `st.hypotheses`), never on the raw
    `FoldCursor` accumulator, so `model_copy(deep=True)` in `snapshot()` stays cheap. `RunState.cards` +
    the three operator-override maps are `default_factory=dict`; these empty `{}` keys **do** appear in
    `model_dump` for all runs → the golden is regenerated and the "adds nothing to the dump" claim is
    corrected.
23. **Cards are ref-shaped, not body-shaped** (hard build gate): `research_origin`=memo-id ref,
    `cross_run_prior`={matched_concepts, prior_run_ids/outcomes} refs, `steering_context`=a compact
    STRUCTURED cue list, and **no code body on the card** (speculative code belongs to its node; reference
    `node_id`). No card field carries verbatim source/captured-output.
24. **Immutable seed statement vs editable display statement.** `card_added` captures an immutable seed
    used as the hash-fallback join key; a later `card_edited` paraphrase overlays DISPLAY only. Feeding an
    edited statement into `hypothesis_id()` would un-link every legacy hash-joined node and vanish the
    card's evidence. Compute the node→card hash-join on the immutable seed BEFORE applying operator edits.
25. `node_building` carries `card_id` as an additive data field (not only on the `Idea`), so
    `_derive_cards` marks build-in-flight from `st.buildings` alone and the producer dedup ("is this card
    already building?") works. This requires minting the card (`card_added`, main-task, under `_id_lock`)
    at/just-before reservation for speculative **drafts** too — decoupling card mint from idea proposal
    (extends §4, which scoped `card_id` to the Idea produced *after* `node_building`).

**E — Provenance, operator-wins, events:**
26. **Card provenance is server-stamped, never client-supplied.** `normalize_control` HARDCODES
    `source='operator'`/`dropped_by='operator'`/`pinned=True` after validating `card_id`, and keeps
    provenance OUT of `CONTROL_DATA_FIELDS` (else a UI forges `source:'novelty'` and defeats operator-wins
    + the freshness gate's engine-vs-operator branch). All four L6 events register across
    `CONTROL_EVENTS`+`COLLABORATION_EVENTS`+`CONTROL_SPECS`(NO_SPAWN/folded_intent)+`CONTROL_DATA_FIELDS`+a
    `normalize_control` branch in the SAME change (the two assert-equal guards, run_commands.py:113/150).
27. **Operator-wins is structural, not arrival-based.** `_derive_cards` applies Strategist `card_ranked` +
    engine `card_enriched` first, then overlays operator maps in a FIXED LAST phase → operator wins
    independent of event arrival order. `card_enriched` must NOT overwrite any field present in
    `card_operator_edits` (or carry an `expected_card_version` CAS).
28. `Card.status` is a DERIVED enum with a FROZEN UI-contract lane vocabulary
    `{proposed|building|coded|running|evaluated|gated|dropped}`, kept OPEN so L5 can add
    `speculating`/`built-awaiting-commit` without a model rework; it must distinguish true-open
    (no node → EXPLORE band) from `gated` (only trust-gated/breed-excluded nodes → excluded, not
    re-proposable as fresh).
29. **Every new `EV_*` lands in exactly one bucket** (`test_event_types.py` guard). **Folded `_HANDLERS`**
    (main-task, additive): `card_added`, `card_merged`, `card_dropped`, `card_enriched`, `card_ranked`,
    `card_build_requested`, `card_build_done`, and the L6 operator events (`card_reprioritized`,
    `card_edited`, `card_resource_pinned`, operator `card_dropped`). **DIAGNOSTIC** (fold-ignored, splice-
    neutral by construction): `EV_CARD_BUILT`, `EV_CARD_FRESHNESS`, optional
    `EV_SCHED_DECISION`/`EV_GPU_ALLOCATED`. **NONE go in `BACKGROUND_APPENDABLE`.**
    `card_build_requested`/`card_build_done` are the ONLY autonomous L5 queue events that affect
    selection and form a generation-fenced request/done counter pair (verbatim mirrors of
    `_on_fork`/`_on_fork_done`). Explicit L6 operator controls may also change later selection/admission;
    an exact post-start operator drop may cooperatively cancel its already-running eval.

**F — Defaults & Layer 2 scope:**
30. Layer 2 scope (**revised by owner decisions 6 & 7**): additive `eval_parallel`/`llm_parallel` fields +
    AUTO(0) decoupling, with the **new names canonical everywhere** and `max_parallel`/`parallel_build`
    demoted to thin back-compat aliases (set-legacy → feeds new; unset → new wins; env vars / snapshots /
    ~100 `Engine(...)` call sites keep the old names alive — they CANNOT be deleted, CLAUDE.md "never
    rename fields"). Drop the property-descriptor rename. Neither new field is in `RUN_START_PINNED_FIELDS`
    (re-read live on resume). A Strategist/operator `*_parallel=0` (or legacy `max_parallel=0`) must still
    yield 1, not GPU-count. **`llm_parallel` is a multi-lane budget**, not a single build pool — named
    lanes `build/propose`, `deep_research`, `novelty_dedup`, `enrichment`, each with a dedicated worker
    allotment; `AUTO` couples the build lane to `eval_parallel` and leaves research unbounded (byte-
    identical default). Unclassified engine-side LLM work enters a closed `engine` fallback lane: it
    still consumes the shared total, so a newly-added producer cannot silently bypass a finite budget.
    A legacy-only `parallel_build` setting/control retains its historical build-only meaning and does
    not enable the shared total; once a canonical live total was recorded, later legacy build retunes
    retain that total in a separate replay receipt so live execution and resume cannot diverge.
    The Strategist re-allocates the total + per-lane split on its cadence
    (`engine/strategy.py`) and steers only the new names. Layer 2 alone still delivers **no** throughput
    change (alternation kept) — its value is the cost-control decoupling + the lane budget that L4/L5 make
    load-bearing. Timeout is ONE canonical field (`eval_timeout`) with a **Settings hard ceiling** the
    agent-declared value is clamped to (decision 3); footprint carries only `gpus`/`mem`.

### 12.3 Landed Card schema and current transport contract

The reason the review ran *before* coding 1a: several later-layer needs are Layer-1 schema items that,
if omitted, force a fold-semantics rewrite. Land these in 1a/1b:

**Structural fields:**
- `Idea.card_id: Optional[str]=None` (round-trips through `durable_idea_payload`→`node_created`→
  `Idea(**d['idea'])` for free — only concept fields are popped). Engine-minted monotonic `card-{k}`,
  main-task-only, planned under process-local `_id_lock` and appended before the separate
  `node_building` claim (ceiling `1+max(card_added ids)`); the two appends are not one transaction.
- `node_building.card_id` as an additive data field (see decision 25); mint the card for speculative
  drafts too, decoupling card mint from idea proposal.
- `Card.status` — derived open string; current replay emits proposed/building/running/evaluated/gated/dropped
  and leaves additional future vocabulary visible rather than rejecting it.
- `Card.evidence`/`best_delta`/`verdict` — computed by the **extracted pure helper** (values,
  never stamped onto Node/Hypothesis).
- **Immutable seed statement** captured on `card_added`, held separate from any editable display
  statement, used as the hash-fallback join key (decision 24).
- Flat action fields `operator`, `params`, `space`, `eval_profile`, `eval_timeout` — with the
  operator KIND populated at `card_added` for pre-built/speculation candidates (so L3/quality can map a
  card to `legal_actions` semantics before any node exists).
- **Prospective parent anchor** `Card.parent_id: Optional[int]` + `parent_ids: list[int]`, captured at
  `card_added`/`card_ranked` — the freshness gate re-derives improve/merge legality for a not-yet-built
  card against `state.best()`/`rank_by_metric[:2]`/`breedable_nodes()`.
- Open, bounded `Card.source` and `dropped_by` strings + `dropped_reason` + `merged_into`/`aliases`
  (cycle-safe via `_canon`); known writer values include researcher/operator/engine/freshness/novelty.
  Dropped and merged-away cards receive terminal/merge `selection_blockers`; L3 must never treat the
  broader compatibility `actionable` set as its queue.
- The score fence is the triple `scored_against`, `scored_against_generation`,
  `scored_against_empty`, plus exact `parent_generations`. With an incumbent, id and generation must
  still match; without one, the writer must explicitly set the empty marker. Missing legacy fence data
  stays unknown and never becomes selection-ready.
- `RunState.cards: dict[str,Card]=default_factory(dict)`, assigned only inside `_derive_cards`.
- **Reserve now** the operator-override maps + the final-overlay phase:
  `RunState.card_priority_pins`/`card_operator_edits`/`card_resource_pins` (`default_factory=dict`), even
  though the pin EVENTS land in L6 — else L6 forces a `_derive_cards` rewrite. These empty `{}` keys
  appear in the dump → regenerate the golden and correct any "adds nothing" claim.

**Landed public read contract (owner state, SSE, and review state).** `RunState.cards` remains an
internal folded model; transport metadata is deliberately not written back into it. The shared
`AppState.state_payload` publishes one canonical fragment on all three surfaces:

- `state.cards` remains the backwards-compatible id-keyed DTO mapping. It is allow-listed and bounded
  to 256 cards, 8 KiB per card, and 512 KiB for the mapping. Fold input journals and operator override
  maps are excluded before serialization; they are not public fields and are not counted as omissions.
- `state.cards_projection` is the separate bounded receipt. Its `total`, `returned`, and `omitted`
  partition the exact folded source mapping (invalid/secret-bearing ids count in `total` but are omitted
  fail-closed); `source_valid` distinguishes an exact empty mapping from a malformed source.
- Collection `complete` is true only when every source card was returned **and** every returned card's
  public fields were exact. `items[id].fields` gives the per-card field partition. Sparse
  `items[id].omissions` receipts give exact source-unit counts for truncated/redacted strings, bounded
  lists, maps, the action block, and concept ownership/tags. A transformed value is not counted as an
  exact returned unit. The metadata sidecar has its own 512 KiB ceiling and participates in admission,
  so it cannot become an unbounded SSE amplification path.
- Existing clients may continue reading only `state.cards`. Completeness-aware consumers must check the
  collection receipt before treating absence as truth and the per-card receipt before treating a DTO as
  exact. Upstream receipts inside `cross_run_prior` and `concept_source` retain their own meaning; this
  outer receipt reports only additional loss at the public Card boundary.

**Enrichment:**
- `Card.footprint` is the immutable receipt-owned declaration/developer provenance, while the operator
  override is a separate `Card.resource_pin` shape `{gpus?, gpu_mem_mib?, pinned_by:"operator"}`.
  UNSPECIFIED (`None`/`gpus None`)
  distinct from explicit `gpus=1` (decision 16). Also `Idea.footprint: Optional[dict]=None` (rides
  `durable_idea_payload` like `eval_profile`). **Timeout is NOT on the footprint** (owner decision 3): it
  stays the single canonical `eval_timeout`, and the agent-declared value is **clamped to a new Settings
  hard ceiling** (`max_eval_timeout`, `agent_control`-gated) — footprint carries only `gpus`/`mem`.
- **Reserve `DEVELOPER_OUTPUT_ATTRS` member `last_footprint`** (slot only; L4 wires it). Note every
  delegating wrapper (`ValidatingDeveloper`, best-of-N, the foresight `__getattr__` panel via
  `forward_hints`) must forward it — reserving now avoids registry-guard churn later.
- `Card.novelty_verdict` (grade/level/near_node/recommendation) from the already-folded
  `novelty_events`/`novelty_grades` (a card FIELD, per §0.5 — never a colliding `card_dropped`).
- `Card.cross_run_prior` (ref-shaped), `research_origin` (memo-id ref), `lesson_refs`/`claim_refs` (id
  refs); `foresight_rank`/`priority`/`confidence` + a `pinned` provenance flag; `concept_tags` plus an
  exact owner/generation/provenance/materialization receipt from folded `node_concepts` (never a mixed-
  evidence union; `card_enriched` may tag only a node-less proposal and cannot self-certify provenance);
  `steering_context` (structured cues only — enforce at the mint
  site, or give it its own `entropy=True` redaction pass, since cards ride only the tokenless public dump).

**Events (1a):** `card_added` (thin — id, immutable statement, source, steering snapshot, operator kind,
parent anchor, `scored_against`; appended immediately at mint), `card_merged`, `card_dropped`. **(1b):**
`card_enriched` (last-write-by-seq), `card_ranked`. **Reserve awareness** that L5 adds folded
`card_build_requested`/`card_build_done` + a durable `card_build_done`→`node_id` link + a per-node
**speculative marker** on `node_created` — the L1 status enum and node schema must be OPEN to these so L5
does not force a rework.

### 12.4 Cross-layer gaps that no single layer owned (now assigned)

1. **Dual-view Node-budget accounting** → L3 owns the effective filtered ranking denominator; L5 and the
   common reservation boundary charge tombstoned, gated, failed, freshness-dropped and outstanding
   speculative reservations to the hard physical ceiling (decision 13). *Critical, resolved.*
2. **Durable speculative marker** on `node_created` + the `card_build_done`→`node_id` link — required by
   the freshness gate/crash-recovery; keying the gate on "`idea.card_id` is set" would fire on NORMAL
   card-linked nodes and break default byte-identity. A Layer-1 schema item beyond §0.5 → owned by 1a
   (reserve) + L5 (emit).
3. **CANCEL of an in-flight eval** on operator/freshness drop → RESOLVED (owner decision 1):
   **burn-to-terminal by default** (the running eval's result is still valid evidence); active-cancel
   (reuse `_evaluate`'s `kill_signal`, train_monitor/asha_monitor precedent) is added ONLY for the operator
   "stop this card NOW" affordance (L5 engine kill + L6 intent), and later if the harness shows real burn.
4. **Immutable-seed vs editable-display** split (decision 24) — an L6-discovered flaw whose fix lands in
   L1's `_derive_cards`; captured in 1a.
5. **Scorer-fidelity** (does the L3 scorer reproduce `next_actions` exactly — `merge_every`/`ablate_every`
   cadence, `operator_bandit` UCB — *before* any staleness) → measured by the deferred quality harness;
   must go green before `speculation_depth>0`.
6. **Producer role-pool isolation** — the producer draws a pooled `(researcher,developer)` pair (L2 owns
   the pool, L5 owns the producer; the contract sits in the seam, decision 5).
7. **`footprint.timeout` vs `idea.eval_timeout`** → RESOLVED (owner decision 3): one canonical
   `eval_timeout`, clamped to a new Settings hard ceiling (`max_eval_timeout`); footprint carries no timeout.
8. **confirm/holdout endgame coupling** (L3 empty-condition ⟺ L5 not keeping the queue non-empty past the
   node-budget boundary) → a shared liveness test on the quadratic toy A/B.
9. **Docker `--gpus` honesty** on GPU-present/no-nvidia-runtime boxes → RESOLVED (owner decision 4): a
   non-empty scheduler-owned device pin is an enforcement boundary, so probe the daemon/runtime and
   **fail closed before launch** when it cannot be honored. Explicit CPU reservations override CUDA-image
   defaults with `NVIDIA_VISIBLE_DEVICES=void`; truly unspecified legacy work remains unpinned.

### 12.5 Remaining risks (with mitigations)

| Sev | Risk | Mitigation |
|---|---|---|
| **crit** | Repeated tombstone/freshness-drop cycles bypass `max_nodes` if the filtered ranking denominator is also used as physical capacity | Keep the filtered effective ranking view, but charge every durable Node id and outstanding request to one hard reservation ceiling; only `budget_extend` adds slots |
| **crit** | Over-declared footprint (`gpus>len(_gpu_ids)`, operator pin `gpus=99/0`) wedges or silently becomes CPU work | Clamp positive requests to a non-empty pool envelope; on a zero-GPU host preserve the positive unsatisfied request; explicit `gpus=0` is the only CPU declaration; re-clamp operator pins on resume |
| high | Sole-writer violation if a worker appends `card_added`/`node_created` | Workers fill only bounded `_spec_builds`/`_spec_raw_stage_result`; main task performs the short reservation/staging CAS and commits the reserved lifecycle; Variant-1 stays seed-only |
| high | Freshness keyed on "rank-1" collapses the population lane + re-idles GPUs | Drop only on merged/dropped/footprint-miss/not-in-selection-SET; L3 exposes its live selection set |
| high | Build-selection ↔ durable-request divergence, or async build-producer crash, wedges the pipeline + every resume | Build producer strictly request-driven; raw proposals must be staged before election; main-task give-up branch appends `card_build_done{skipped}` |
| high | Developer-crash sentinel eval race (consumer dispatches on a `'(developer error:'` node) | Exclude sentinel-code nodes from the consumer readiness predicate (fold-readable check) |
| high | Early-finish + repair-starvation under L3 (non-forced `[]` → confirm with budget unspent; dropped `debug_action` starves a failed leaf) | `card_next_actions` never non-forced `[]` while budget remains (fall back to `next_actions`); bake `debug_action` into the forced prefix; liveness test reaching confirm at the same budget as the serial spine |
| med | `eval_parallel` mis-modeled as GPU inventory re-seeds `--gpus 2`; defaulted `gpus=1` pins a serial DataParallel run | `eval_parallel` = outer ceiling only; pack against `_free_gpus`(+mem); UNSPECIFIED = today's per-branch behavior |
| med | ASHA speculation across rung boundaries promotes a non-survivor | Cap ASHA speculation to depth-0 across rungs; merge freshness = both parents in `rank_by_metric[:2]` AND breedable |
| med | Forgeable provenance + unbounded operator pin (L6) | Server-stamp provenance out of `CONTROL_DATA_FIELDS`; clamp pin to envelope (400); register all four events in one change |
| med | `_derive_cards` verdict-before-merge diverges a merged card's verdict silently | Pin the mirror order (merge-union before verdict); characterization test with a `card_merged` alias chain asserting byte-equal verdict |
| low | `steering_context`/any raw card field ships on the tokenless public dump — a captured secret leaks | Contract-enforce structured-only cues at the mint site; ref-shape as a hard gate; card-trim step + ref-size cap test if any raw field is ever added |

### 12.6 Implementation stages (the ordered build plan)

Dependency- and risk-ordered. Each autonomous stage is behind a flag/default whose value == today; L3 is
the baseline selector replacement, while positive-depth L5 execution and L6 operator commands are
separately gated. Resources (L4) are settled before any of them consume the envelope. **Note the reorder
vs §0's 1→6:** the split Layer 1 (1a-extract →
1a-model → 1b → 1c), then **2 → 4 → 3** (resources before the selection change, since L3 population lanes
need `eval_parallel` and footprint-fit), then **5a → 5b** (concurrent core, then speculation+freshness),
then **6**.

| # | Stage | Changes selection? | Risk | Depends on |
|---|---|---|---|---|
| 1 | **1a-extract** — pure verdict-helper extraction (byte-identity tripwire) | no | low | — |
| 2 | **1a-model** — Card model + `_derive_cards` + `card_added/merged/dropped` + `node_building.card_id` + card-mint decouple | no | med | 1a-extract |
| 3 | **1b** — card enrichment fields + `card_enriched`/`card_ranked` + reserve `last_footprint` | no | med | 1a-model |
| 4 | **1c** — dropped/merged exclusion + `gated` distinction | no (advisory) | low | 1a-model, 1b |
| 5 | **2** — two independent concurrency pools (knob split, no overlap yet) | no | med | — (orthogonal) |
| 6 | **4** — agent-declared footprint + mem-aware bin-packing admission | no | high | 1b, 2 |
| 7 | **3** — card-driven selection scorer (**CHANGES SELECTION**, flag-gated) | **yes** | high | 1a-model, 1b, 1c, 2 |
| 8 | **5a** — concurrent producer/consumer substrate (strictly inactive at `speculation_depth=0`) | no (default) | high | 2, 3, 4 |
| 9 | **5b** — pre-launch freshness gate + speculation-depth enablement | yes, only with Card selection + positive depth | high | 5a, 4, 3 |
| 10 | **6** — board UX (card Kanban) + operator card-steering | yes, only on explicit operator command | med | 1a-model, 1b, 1c, 3, 4, 5b |

**Per-stage scope / gate / tests (the load-bearing detail):**

- **1a-extract.** Extract a VALUES-returning pure helper (`_evidence_verdict`) covering the run-global
  record-setter set (replay.py:2969–2977) + the per-evidence `best_delta`/`supported`/`status` block
  (replay.py:2980–3012). No card field. *Gate:* mechanical refactor — `test_golden_replay` GREEN **without
  regen**; any hypotheses-subtree diff proves impurity and is the tripwire. *Tests:* golden unchanged +
  a NEW characterization test over a hand-built log (merge/`_canon` chain, abandoned, record-setter-later-
  overtaken, testing-vs-open) asserting `fold(evs).hypotheses` byte-equal pre/post AND `st.nodes` unmutated.
- **1a-model.** `RunState.cards` (assigned only in `_derive_cards`), `Idea.card_id`, the three override
  maps; the full `Card` (id, immutable seed, source, dropped/merged, idea block, parent anchor,
  `scored_against`, evidence via the helper); `card_added/merged/dropped` in `_HANDLERS`+registry;
  `node_building.card_id`; mint at/just-before reservation so drafts carry `card_id`; `_derive_cards` in
  `_finalize_fold` after `_derive_hypotheses`, in the pinned mirror order. *Gate:* old logs → `cards=={}`/
  `card_id null`/empty maps; cards advisory. *Tests:* golden **regen** (one documented additive diff),
  `test_event_types`, `test_events_replay` prefix-stability/idempotence, NEW: card-built node links by
  `idea.card_id` (not hash), two concurrent draft builds dedupe via `node_building.card_id`, merged-card
  verdict == hash-joined hypothesis verdict byte-for-byte.
- **1b.** `Idea.footprint` + `Card.footprint` sub-schema (UNSPECIFIED distinct from `gpus=1`),
  `novelty_verdict`, `cross_run_prior`, `research_origin`, `foresight_rank`/`priority`/`confidence`+pinned,
  `concept_tags`/exact `concept_source` receipt/derived `provenance_tier`, `steering_context`;
  `card_enriched`(last-write-by-seq, proposal tags remain untrusted)/`card_ranked`;
  reserve `last_footprint`; structured-only cue enforcement; `footprint.timeout` governance gate. *Gate:*
  all additive/nullable; ref-shape hard gate (no body fields). *Tests:* `test_event_types`, golden regen
  for `footprint:null`, NEW: ref-size cap, `steering_context` passes redaction, `test_role_output_contract`
  green with `last_footprint` reserved.
- **1c.** Wire engine-authored `card_dropped` (novelty/freshness; node-less `_intra_batch_dup`) +
  `card_merged`/`_canon` into `_derive_cards` exclusion; derive `status='gated'` for all-breed-
  excluded/trust-gated evidence (distinct from true-open); tolerate a card whose only node terminated
  `node_failed(reason='superseded')`. *Gate:* advisory only. *Tests:* dropped/merged/gated are not
  `selection_ready` (while compatibility `actionable` retains its documented legacy meaning);
  gated≠EXPLORE-band; `card_dropped` order-tolerance applied LAST.
- **2.** `Settings`+`EngineOptions` `eval_parallel`/`llm_parallel` `Optional[int]=None` as the **canonical**
  fields; resolve `_eval_parallel` (None→legacy `max_parallel`, AUTO(0)→`max(1,len(_gpu_ids))`) and
  `_llm_parallel` (None→legacy `parallel_build`, AUTO(0)→`_eval_parallel` for the build lane). `max_parallel`/
  `parallel_build` are **thin back-compat aliases only** (set-legacy → feeds new; env vars / snapshots /
  ~100 `Engine(...)` sites keep them alive — not deletable). `llm_parallel` is a **multi-lane budget**
  (`build/propose`, `deep_research`, `novelty_dedup`, `enrichment`) — build lane couples to `eval_parallel`
  at AUTO, research lane defaults unbounded (byte-identical). **Strategist grants target the NEW names only**
  (raw store, no AUTO re-resolve, `max(1,int)`); the periodic per-lane re-allocation rides `engine/strategy.py`.
  Docs + settings-UI field count + diagram parallelism boxes. *Gate:* all None → legacy 1/1 byte-identical,
  research overlap unbounded as today; not in `RUN_START_PINNED_FIELDS`; zero fold impact. *Tests:*
  `test_engine_options` identity, golden unchanged, NEW: Strategist/operator `*_parallel=0` (and legacy
  `max_parallel=0`)→1 (not GPU-count), accessors never raise on a built engine (getter-typo → red, not
  silently masked to 1), NEW: per-lane budget split honored + research-lane default preserves unbounded overlap.
- **4.** Wire `last_footprint` end-to-end (Developer sets; engine merges onto the Idea at the four
  `_emit_node_created` sites like `last_files`; forward through every wrapper); build `_gpu_mem` from
  `core/hardware.detect_gpus()` joined to `_detect_gpu_ids` (logical↔physical remap under
  `CUDA_VISIBLE_DEVICES`; degrade to count-only on join failure); replace `_acquire_gpu`/`_release_gpu`
  with `_acquire_gpus(n,mem)`/`_release_gpus` (all-or-nothing under `_gpu_lock`; clamp positive
  over-declaration only against a non-empty pool; a zero-GPU host preserves it as unavailable);
  footprint-aware admission in `_dispatch_evals` with a race-free primitive + pop-first-fitting so
  `gpus=0` skips a blocked head; `eval_parallel` = outer ceiling only; a saturated unspecified parallel
  request waits rather than oversubscribing; reservation held across the node lifecycle, released once in
  the dispatcher `finally`; Docker `--gpus device=` remap with daemon/runtime probe and fail-closed positive
  pinning, plus `NVIDIA_VISIBLE_DEVICES=void` for explicit CPU isolation; make
  proposal_cues/crash_repair `--gpus 1` cue footprint-CONDITIONED (PromptStore contracts, changed
  deliberately); confirm-phase seed evals acquire through the same pool. *Gate:* UNSPECIFIED → today
  byte-identical; footprint never enters metric/best/`next_actions`/breedable; ordinals never fold.
  *Tests:* over-declared clamps without wedging; unspecified serial multi-GPU stays whole-box; race-free
  admission under concurrent releases; `gpus=0` past a stalled head; Docker remap, fail-closed pinning and
  CPU isolation;
  agent-declared `eval_timeout` clamped to the `max_eval_timeout` Settings ceiling (never exceeds it);
  Developer scale-up beyond the Researcher proposal allowed but clamped to the pool envelope + cue;
  `test_role_output_contract` green with all wrappers forwarding; golden unchanged.
- **3.** `Settings.card_driven_selection=False`; a third arm at orchestrator.py:1006 (explicit if/elif with
  pinned precedence vs `agent_drives_actions` + a both-flags test); `card_next_actions(state,policy,
  max_nodes)` reusing `legal_actions` forced gates in EXACT order (pending→evaluate-all, `debug_action`
  FORCED, `max_nodes`→`[]`, seed-wide, then card scoring; never a non-forced `[]` while budget remains);
  optional additive `card_score`/`card_select` per policy (a policy lacking `card_score` falls back to
  `next_actions`); greedy=argmax single hot card, population=top-K lane (documented intended flag-on change
  for mcts/asha, or node-granular improve within the lane); open-band lexicographic `(band, exploration_
  key)`; **the ranking denominator excludes tombstoned/gated nodes while one raw reservation ceiling
  charges them** (shared fix, pinned here); optional
  `card_selected` audit as **DIAGNOSTIC** (owner decision 5; promote to REPLACE-latest folded only if the
  board later needs it — never an unbounded per-iteration list); **`Strategy.card_scoring` is a distinct
  field the Strategist shapes** (owner decision 7 — explore/exploit stance + weights on novelty/coverage;
  `validate_strategy` whitelist), not merely derived from the policy. *Gate:* flag off → byte-identical; flag
  recorded in `run_started`; empty-actions contract identical to `next_actions`. *Tests:* liveness (no
  early finish), `debug_action` before any card improve, greedy single-hot-card == serial greedy, mcts/asha
  intended delta, both-flags precedence, dropped/gated never scored, effective ranking excludes them but
  the hard physical ceiling does not refund them,
  `test_options_divergence`+golden unchanged at default.
- **5a.** Replace the build/eval tail (orchestrator.py:1050–1162) with one structured session using
  scoped `anyio` task groups, modeled on the `_dispatch_evals` continuous-dispatch loop; outer loop
  (836–1048) + `_spawn_research` overlap
  byte-identical; CONSUMER = generalized continuous-dispatch (re-fold, pick READY card via L3 scorer,
  EXCLUDE sentinel nodes, admit via L4, terminals stay main-loop under `_write_lock`); PRODUCER =
  request-driven, isolated pooled role pair, fills `_spec_builds[(K,G)]`, no folded event; MAIN task:
  elect + `card_build_requested` (folded, `_id_lock`, generation-fenced; `_on_card_build_requested`→
  `st.card_build_requests`), `_serve_card_builds` spine branch (crash-recovery-first; else
  `_create_node(precoded=…)`+speculative marker+`card_build_done{node_id,speculative:true}`; else
  `card_build_done{skipped}`); add a precoded-result path to `_create_node` (sibling of `preproposed`);
  when no ready Card owns a built-in counterfactual raw-create lane during an eval, a worker-only isolated
  Researcher prepares it without folded writes and the main task stages it only after
  generation/parent/best/cue/tail-CAS revalidation, then requests that exact Card; custom/ablation/invalid
  raw lanes yield once to the serial outer spine; quiescence includes folded state plus buffered/in-flight
  build, raw-proposal and eval latches; keep every fold/policy/slow call outside `_id_lock` so its blocks
  contain only the short tail-CAS append; selection excludes freshness-dropped speculative nodes while
  their physical reservation remains charged.
  *Gate:* `speculation_depth=0` → never elects, alternation
  byte-identical; folded-but-never-emitted keeps golden/`test_options_divergence` green; new events recorded
  in run_started-pinned settings. *Tests:* sole-writer, request-driven fill exactly `(K,G)`, producer-
  failure give-up (no wedge, survives resume), crash between `node_created` and `card_build_done` recovers
  without double-create, quiescence never declares drain with a build in flight, sentinel never GPU-
  dispatched, raw fallback overlaps a held eval and rejected raw proposals run once then yield,
  structural `_id_lock`-scope checks plus injected tail-CAS races prove slow fold/proposal work is outside
  the lock and lifecycle events are not duplicated; `test_background_appendable` unaffected;
  `test_event_types`.
- **5b.** Freshness gate in the consumer before every scorer consult and pre-GPU (pure predicate over the
  fresh fold for speculative-marked nodes; DROP only on merged/dropped/footprint-miss/not-in-selection-
  SET); on drop append `node_failed(reason='superseded', eval_seconds=0)` under `_write_lock` + optional
  `DIAGNOSTIC EV_CARD_FRESHNESS`; `speculation_depth: int=Field(default=0,ge=0,le=64)` (0=OFF;
  1–64 = exact static live-prefetch-backlog depth; adaptive/AUTO depth is deferred); a positive value is
  effective only with `card_driven_selection=True`; live depth = outstanding requests + committed pending
  speculative nodes not already admitted to this exact consumer session; cap ASHA
  speculation to depth-0 across rungs; merge fresh iff both parents in `rank_by_metric[:2]` AND breedable;
  keyed on the durable speculative marker + `card_build_done`→`node_id` link (NOT "`idea.card_id` set").
  *Gate:* `speculation_depth=0` → gate never fires, byte-identical; gate scoped to speculative-marked
  nodes only. *Tests:* under `eval_parallel>1` the eligibility gate does NOT drop N-1 of N; a source-order
  tripwire proves the outer freshness drain precedes `_select_actions`, while the consolidated gate runs
  the existing confirm/holdout suites for the unchanged outer finalization path; ASHA depth-0 across
  rungs; merge both-parents rule;
  dropped speculation does not refund a physical reservation; freshness re-run on resume drops a now-stale
  node; only `budget_extend` admits another slot; golden unchanged at depth 0.
- **6.** Extend `ui/src/panels.jsx::HypothesisBoard` into a card Kanban keyed on derived `Card.status`
  lanes (reuse `byStatus`/priority sort, optimistic-override, evidence chips→`onSelect`); trim card
  payloads in `state_payload` paralleling the node-trim loop; register FOUR events across all registries in
  ONE change; server-STAMP provenance (out of `CONTROL_DATA_FIELDS`); CLAMP `card_resource_pinned` to the
  envelope (400; re-clamp on resume); fold into the reserved override maps overlaid LAST (operator-wins);
  `card_edited` overlays DISPLAY only (join key stays the immutable seed); `card_enriched` must not clobber
  operator edits (or CAS); treat `card_edited` as potentially selection-affecting (live scorer re-reads
  next pass); under the append lock re-fold and recheck generation, canonical Card existence and the live
  resource envelope; preserve global event-order LWW when aliases later merge; before serial/speculative/
  confirm GPU launch, re-fold the effective pin and release/retry any reservation formed for an older pin;
  docs (config/UI field counts, `agent-architecture.html` board box). *Gate:* NO_SPAWN/folded_intent never
  starts or wakes compute and never changes a request/done counter; the sole intentional runtime side
  effect is an exact post-start operator `card_dropped`, which cooperatively cancels its already-running
  eval and charges elapsed compute. Generation is fenced via the existing `runCommand expected_generation`
  and the append-time CAS. *Tests:* run_commands assert-equal guards green; `test_event_types`; forged
  `source:'novelty'` rejected/overridden; out-of-envelope pin →
  400 (no unschedulable card folded); `card_edited` paraphrase does NOT break the hash-join; operator pin
  survives a Strategist re-rank + a Developer re-finalize; `card_enriched` does not clobber operator edits;
  append-time subject/envelope races fail closed; aliases retain global log-order LWW; pin changes between
  reservation and launch release/retry in serial, speculative and confirm paths; only the exact post-start
  operator drop cancels a running eval, while engine/unrelated/pre-start drops do not.

### 12.7 Mandatory rollout gates and truly deferred follow-ups

> This section is the original 2026-07-20 **target contract** for the rollout gates. Its "mandatory
> before rollout" items are satisfied for the calibrated Toy scope by the receipt and checked live TODO
> in §0.0.1 (the current ledger) — treat §0.0.1 as the single source of rollout-authority status. The
> requirements below still bind a rollout to any *new* workload, which must regenerate its own evidence.

**Mandatory before any `speculation_depth>0` rollout (implemented; validated for the calibrated Toy
scope per §0.0.1 — a rollout to a new workload regenerates this evidence against its own profile):**

- **Search-quality measurement gate**: bounded strict replay of exactly three fresh depth-0/positive-
  depth pairs for fixed seeds `0/1/2` from the source-owned offline calibration profile. It requires
  the exact clean event allow-list, attempt-zero node lifecycle, complete finalization suffix and a
  real CUDA context plus 4096-byte allocation/free proof on every evaluated node; re-derives
  hit/divergence rate, normalized regret and non-vacuous trusted concept coverage from raw
  event/config/task evidence; binds implementation, environment, seven-field effective GPU identity,
  Greedy policy, exact tested depth, `max_nodes`, complete runtime scope and deterministic quadratic
  Toy-task profile; and emits a tamper-evident receipt only when every fixed threshold passes.
- **Scorer-fidelity gate**: 15 exact ordered cases cover the forced pending/seed/debug/budget prefix,
  min/max direction, merge and ablation cadence boundaries, and untried/unequal-count bandit ownership.
  Any semantic or action-ownership mismatch blocks the quality receipt.

The clean event allow-list above is an **evidence protocol**, not a removal of Layer-6 operator control.
A public run admitted by a current receipt may still accept the generation-fenced Card controls described
in Stage 6. The first such intervention deliberately takes that run outside the calibrated A/B evidence
envelope: its log cannot be used to issue, refresh, or substitute for a rollout receipt, and the quality
reader rejects it. The receipt authorizes the pinned mechanism/configuration; it does not claim that an
operator-modified trajectory is one of the six clean calibration trajectories.

**Deferred (not authority granted by the narrow receipt):**

- ~~**Unified `llm_parallel` CapacityLimiter**~~ — **PROMOTED to in-scope by owner decision 6.** The
  build fan-out, `_spawn_research`, `novelty_dedup`, and enrichment share one budgeted, Strategist-
  allocated limiter with named lanes. Folding research under a limiter can't be byte-identical, so it
  lands behind a knob whose **default preserves today's unbounded `_spawn_research` overlap** (each lane's
  AUTO = "unbounded" for research until the Strategist/operator sets a finite split). Built as an
  extension of L2 (the lane structure) + the Strategist mixin (the periodic re-allocation).
- **Retiring `HypothesisBoard`/`state.hypotheses`** from the public dump — deferred to a post-L6
  deprecation window; the cards+hypotheses dump duplication is accepted through L1–L6.
- **Adaptive speculation depth** (keep the ready buffer ≈ `eval_parallel` dynamically) — ship the static
  `speculation_depth` const first.
- **General-workload/policy rollout** — the first receipt is intentionally limited to the exact Greedy,
  deterministic quadratic Toy-task and tested-depth scope. Production tasks and other policy/config
  envelopes require their own representative protocol and evidence; no receipt scope is inferred.
- **Operator "launch this card NOW" HITL** (the sole `ENSURE_RUNNING`/engine-ack card event needing a
  generation-fenced counter) and operator "merge these two cards" — deferred; `EV_INJECT_NODE` already
  covers manual node creation.

**Owner decisions — RESOLVED (2026-07-20).** All seven closed; two of them (6, 7) expand the Strategist
into a **dynamic resource + priority allocator** and are reflected back into the finalized decisions and
the stage scopes.

1. **Cancel of an in-flight eval** → **burn-to-terminal by default.** A freshness/operator drop marks the
   card, but an already-dispatched eval runs to its own terminal — its result is still valid evidence, so
   it is not waste. Active-cancel (reuse `_evaluate`'s `kill_signal`, train_monitor/asha_monitor
   precedent) is added ONLY for the operator "stop this card NOW" affordance and, later, if the quality
   harness shows meaningful GPU burn on stale speculations. Owned by L5 (engine kill path) + L6 (operator
   intent).
2. **Speculatively code dependency-bearing cards → YES (option B).** The producer pre-builds
   improve/merge/debug against the current-best parent, not drafts-only, and relies on the freshness gate
   to drop them if the parent is superseded — so GPUs fill even in the exploit phase. Conservatism stays
   exactly where the review pinned it: **ASHA speculation capped to depth-0 across rung boundaries**, and
   merge-card freshness requires **both** parents still in `rank_by_metric[:2]` AND breedable (decision
   12). Accepts some discarded speculative builds as the cost of no-idle GPUs.
3. **`footprint.timeout` and `idea.eval_timeout` → ONE canonical field, with a hard Settings ceiling.**
   Collapse the two into a single per-eval timeout (footprint carries only the resource declaration
   `gpus`/`mem`; the timeout is the existing `eval_timeout` slot). Add a **Settings hard cap**
   (`max_eval_timeout` / the existing `agent_control` ceiling) that the agent-declared timeout is
   **clamped to** — an agent or operator can request a longer eval but can never exceed the run-configured
   maximum. No second channel, no ungoverned override.
4. **Over-declaration posture → agreed.** The Developer MAY finalize a LARGER footprint than the Researcher
   proposed (the Developer actually knows the code — e.g. a DDP finetune), **clamped to a non-empty pool
   envelope** with a cue/log so the scale-up is visible. On a zero-GPU host a positive requirement remains
   positive and unavailable rather than becoming CPU work. Docker untrusted/hostile tiers must
   **fail closed** when a non-empty device pin cannot be enforced; only explicit `gpus=0` selects CPU and
   forces `NVIDIA_VISIBLE_DEVICES=void`.
5. **`card_selected`/`card_build` audit bucket → DIAGNOSTIC** (fold-ignored) on first ship; promote to a
   folded REPLACE-latest slot (like `policy_decision`) only if the board later needs "why this card" as
   durable derived state. Zero replay surface now.
6. **`llm_parallel` becomes a Strategist-allocated, multi-lane LLM budget** (expands the AUTO decision and
   L2). Not a single build pool: it is a total LLM-concurrency budget partitioned into **named lanes** —
   `build/propose`, `deep_research`, `novelty_dedup`, `enrichment` — each with a small dedicated worker
   allotment so background research / novelty-dedup / consolidation always make progress alongside builds.
   `AUTO(0)` default still couples the build lane to `eval_parallel` (cost ≈ today); an explicit value or
   the Strategist overrides it. **The Strategist periodically re-prioritizes** (on its existing cadence,
   `engine/strategy.py`) both the total budget and the per-lane split — "what to do next and how much LLM
   goes where." This **promotes the deferred unified-limiter** (see below) from out-of-scope to in-scope,
   behind a default that preserves today's unbounded `_spawn_research` overlap for byte-identity.
7. **New names are canonical everywhere; legacy is demoted to a compat shim; the Strategist owns the
   scoring function.** The Strategist steers the NEW names (`eval_parallel`/`llm_parallel` + its per-lane
   split) directly, and all new engine/scheduler code keys on them. `card_scoring` is a **distinct
   `Strategy` field** the Strategist can shape (explore/exploit stance, weights on novelty/coverage) — not
   merely derived from the policy. *Hard constraint (honest scoping):* `max_parallel`/`parallel_build`
   **cannot be deleted** — `LOOPLAB_<FIELD>` env vars, config snapshots, and ~100 `Engine(...)` test call
   sites + golden replay depend on the exact names (CLAUDE.md: "never nest or rename fields"). So "minimum
   legacy everywhere" is realized as: legacy names survive ONLY as thin back-compat aliases that feed the
   new canonical fields (set-legacy → writes new; unset → new wins); nothing new is built on them, the
   Strategist never emits them, and docs/UI present only the new names. Removing them entirely is a
   separate post-migration deprecation window.
