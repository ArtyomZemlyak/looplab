# Hypothesis-Card Kanban re-architecture — design + Layer 1 detail (2026-07-20)

Status: **proposal, pre-implementation.** Grounded in four exhaustive code maps
(idea-pipeline · strategist/policy · execution/GPU · replay/UI). The high-level plan lives as a
private artifact; this is the committed working record and the concrete Layer-1 design.

## 0. Why, and the build order

Today the spine strictly alternates `build-batch → eval-batch`; in steady state every policy proposes
**one** node per iteration (almost always `state.best()`), so GPUs idle during every propose. The
hypothesis board (`state.hypotheses`, `Hypothesis.priority`) is **derived every fold and never read by
node selection** — it is advice, not a work queue. Resources are hardcoded (1 GPU per eval), which is
why the `--gpus 2` prod bug was unfixable by the loop.

The pleasant surprise (map D): the board is **already** a fold projection (`_derive_hypotheses`),
**already** serialized to the UI, and **already** steerable (`hypothesis_*` + `set_strategy` control
events); `ui/src/panels.jsx::HypothesisBoard` is a shipping kanban. So this is *extending a
load-bearing seam*, not building from scratch.

**Build order — do all of it, in 6 layers by dependency + risk** (each self-standing, behind a knob
whose default == today, each through mega-analysis → build → mega-review → tests → push; risk rises to
the end so the only genuinely selection-affecting change lands last on a settled base):

1. **Data — the card** (this doc). First-class durable card entity + land every homeless signal.
2. **Two pools** — split LLM-concurrency (`llm_parallel`) from GPU-concurrency (`eval_parallel`).
3. **Brain — scoring** — the card queue drives selection; Strategist sets direction, policies score.
4. **Resources** — agent-declared footprint + bin-packing scheduler; fixes `--gpus` by construction.
5. **Execution without idle** — concurrent producer/consumer, speculative pre-build, freshness gate.
6. **Board UX** — extend the kanban to the full lifecycle + card-steering control events.

Owner decisions locked: card accretes fields until ready (readiness is *derived*, not a field);
footprint = an accreted resource field — **Researcher proposes it, Developer finalizes from code**;
speculation depth = enough to keep GPUs utilized, freshness gate on; **stable `card_id` immediately**.

## 0.5. Design-review resolutions (2026-07-20) — AUTHORITATIVE

Two adversarial design reviews (replay-safety + completeness) found real defects; **this section
supersedes any flagged claim in §3–§8 below.** Resolutions:

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

**`card_id` — monotonic, main-task-only, atomic (resolves the load-bearing defect).** A monotonic
`card-{k}` id CANNOT be background-appendable (a bg mint races on `max+1`; the splice test cannot catch
a write-time id race — it only proves fold-order tolerance of an already-written event). Decision:
engine-minted monotonic, **main-task-only**, reserved+appended **atomically under the shared
`_id_lock`**, ceiling `1 + max(card_added ids in log)` — exactly like node ids. `card_added` is **thin**
(id + statement + source + steering snapshot, all available at proposal) and appended **immediately at
mint**; heavy enrichment defers to `card_enriched`. NEVER mint → slow work → append (TOCTOU). Resume is
deterministic (counter = max-over-log + 1). All Layer-1 card events are main-task-written; background
enrichment (Layer 5) will reserve ids up front under the lock.

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

**In scope:** a first-class, durable, folded **Card** entity with a stable id; a `_derive_cards`
projection modeled on `_derive_hypotheses`; new advisory `card_*` events; nodes carry `card_id`; every
homeless producer output (§7) attached to its card; the card store serialized to the UI (rides the
existing public dump for free).

**Explicit non-goals for Layer 1 (deferred to later layers):** NO change to node selection
(`next_actions` untouched); NO speculative build; NO scheduler/resource change; NO concurrency change.
The card store is still **advisory** — exactly like the hypothesis board today. Default behavior is
byte-identical to today; the card store is pure additive folded state.

## 3. The Card model

A new `Card` (pydantic, `core/models.py`) — the union of today's thin `Hypothesis` (`models.py:555-575`)
and the rich substance that lives on `Idea`/`Node`. `RunState.cards: dict[str, Card]` is **derived**
each fold by `_derive_cards` (§6), mirroring how `RunState.hypotheses` is derived today.

| Card field | Filled by (producer → today's home) | Layer-1 status |
|---|---|---|
| `id` (opaque, stable, wording-independent) | engine-minted at `card_added` (§4) | new |
| `statement`, `rationale`, `source` | `Idea.hypothesis`/`rationale` + `hypothesis_added.source` | reuse |
| `idea` block: `operator, params, space, eval_profile` | `Idea` (`models.py:166-218`) via the built node | link |
| `concept_tags` + `provenance_tier` | `Idea.concepts` → classifier/operator (`node_concepts`) | link |
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

All Layer-1 card events are **advisory** (board = "transient advice; selection is what replay pins",
the license already granted at `types.py:274-283`) — none are selection-affecting, so none touch
`next_actions`/best-selection. Modeled byte-for-byte on `hypothesis_added/updated/ranked/merged`.

| Event | Bucket | Written by | Models on |
|---|---|---|---|
| `card_added` (id, statement, source, footprint-proposal, steering snapshot) | folded, `BACKGROUND_APPENDABLE`-eligible | engine (may be a bg enrich task) | `hypothesis_added` |
| `card_enriched` (novelty verdict / cross-run prior / concept tags / footprint-finalize) | folded, advisory | engine | `hypothesis_updated` |
| `card_ranked` (priority order) | folded, advisory | engine (foresight) | `hypothesis_ranked` |
| `card_merged` (alias → canonical) | folded, advisory | engine consolidation | `hypothesis_merged` |
| `card_dropped` (reason) | folded, advisory | engine / operator | `hypothesis_updated`(deleted) |

Each new `EV_*` constant goes in `events/types.py`, added to `replay._HANDLERS` (folded), and — for the
subset written by the concurrent enrichment task — to `BACKGROUND_APPENDABLE` **only after** its
membership is proven by extending `tests/test_background_appendable.py` (splice-position invariance).
`tests/test_event_types.py` guards the one-bucket rule. (In Layer 1 we may keep all card events
main-task-written and defer `BACKGROUND_APPENDABLE` membership to Layer 5 when a background enrich task
exists — chosen at review time.)

## 6. The `_derive_cards` fold projection

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

- `RunState.cards` is `Field(default_factory=dict)` → old logs fold to `{}`; `Idea/Node.card_id`
  defaults `None`. The golden-replay `model_dump()` gains additive `cards: {}` (+ `card_id: null`) →
  regenerate the golden snapshot in the same change (documented, single additive diff), like the
  `buildings` change.
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
