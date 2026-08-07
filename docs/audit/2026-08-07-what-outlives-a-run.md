# What outlives a run — an audit of LoopLab's durable memory

*2026-08-07. Scope: everything the lab accumulates across runs — lessons, cases, meta-notes, the
authoring plane (knowledge / skills / prompts), the concept subsystem, and the retrieval that carries
any of it back into a live run. The in-run search loop (cards, research, developer, evaluation) is
audited separately.*

Every number below was measured against the real store at `/home/jovyan/data/looplab-memory`
(read-only; mutating checks ran on copies) and the 49 event logs under `looplab/runs/`, or produced by
running the shipped engine. The ten product questions already parked in
`looplab-open-questions.md` are not repeated here.

---

## 0. The one-paragraph answer

Distillation is **good**. Retrieval is **broken**, and the store is **98% offline-toy artefact**.

The single best evidence in the whole corpus: `runs/rubertlite-dense-retrieval` distilled 24 lessons
like *"Decoupled Contrastive Learning eliminates gradient starvation at low temperatures"* and
*"Lower weight decay (0.01 vs 0.1) significantly improves retrieval for small transformer models"*.
That is exactly the product promise. **None of them is in `lessons.jsonl`.** What is in
`lessons.jsonl` is 136 rows of `changing x 0.2255->0.4047 regressed the metric by 1.227` — a template
only the *no-LLM* code path can emit. And the retrieval layer that decides what a new run sees is,
under shipped defaults, unable to tell a relevant lesson from a nonsensical one: a query fingerprint
of `kind:banana / zebra / xylophone` retrieves 16 lessons at higher similarity than a genuine
Russian-reranker query does.

So the operator's "pile of junk with duplicates" is empirically right, and the cause is not the
distiller.

---

## 1. The corpus, measured

| Tier | File | Rows | Distinct | What it actually is |
|---|---|---|---|---|
| Lessons | `lessons.jsonl` | **154** | 154 exact keys, **47** number-collapsed templates | 88% machine template |
| Meta-notes | `meta_notes.jsonl` | **159** | 70 texts, **16** number-collapsed shapes | 98.7% machine template |
| Cases | `cases.jsonl` | **9** | 9 (one per `task_id`) | a leaderboard, not a history |
| Research claims | `research_claims.jsonl` | 220 | 101 carry a `statement`; 119 are bare `source_receipt` rows | |
| Concept capsules | `concept_capsules.jsonl` | **3** | — | against 46 runs / 15 tagged |
| Concept aliases | `concept_aliases.jsonl` | **absent** | — | zero governance ever applied |
| Auto-skills | `skills/` | **50** | — | see §2.10 |
| Knowledge | `looplab-knowledge/` | **0 files** | — | `kb_search`'s index is 9 cases and nothing else |

### 1.1 Lessons: what the 154 rows are

Every statement was classified by the f-string that could have produced it:

| Shape | Rows | Producer | Reachable when |
|---|---|---|---|
| `changing x A->B … regressed/improved the metric by D` | **105** | `lessons_reconcile.py::_fallback` → `param_credit_statement` | **only** `client is None` |
| `op 'draft' with params {…} reached M` | **31** | `lessons_distill.py::_winner_lesson` | **only** `client is None` |
| free prose | 18 | LLM reflection / comparative | a run with a model |

`lessons_reconcile.py:118-119` and `lessons_distill.py:242-249` both say so in as many words
("*a real run whose LLM returned nothing usable writes no comparative lesson rather than a
template*"). So **136 of 154 rows (88%) prove their own provenance: they came from runs with no
model wired.** Of the remaining 18, two are agent-merged paraphrases of templates, leaving ~16 rows
of genuine transferable knowledge — **10% of the store**.

- 118 of 154 rows are near-duplicates of a sibling *by digits alone* (they share a
  number-collapsed template with ≥1 other row).
- `consolidate_lessons()` over the real file returns **154 rows — it merges nothing**, correctly:
  its identity key keeps digits on purpose, and each of those 105 rows is a distinct measurement.
  The dedup pass is not failing; there is simply nothing for it to merge.
- Meta-notes are worse: **157 of 159** are the `best metric M via op 'X' params {…}; N nodes, M
  evaluated` stats line, i.e. the fallback taken when `_causal_meta_note` has no client. Only **2**
  are LLM prose.

**The store is a record of the smoke-test suite, not of the lab's research.**

---

## 2. Findings, ranked by cost to the operator

### R1 — Retrieval cannot distinguish a relevant lesson from an irrelevant one. *(highest cost)*

`engine/lessons_priors.py:171` gates lessons on a fingerprint Jaccard of `>= 0.34`. That is the only
relevance gate in the design. Measured on the real store with a brand-new task fingerprint:

```
new tabular-classification task   →  0 of 154 lessons pass the Jaccard gate
```

…and the Researcher's prior is nevertheless **full**, with five `blob_classification` / `toy_quadratic`
parameter rows. All five came in through `retrieve_lessons_harmonic`
(`engine/lesson_hygiene.py:224`, spliced at `lessons_priors.py:182`), which is **ON by default**
(`memora=True`) whenever a run is started through the CLI.

That channel has no working relevance floor. Its `min_score=0.15` was calibrated for semantic
embeddings, but `embed_model` defaults to `None`, so `make_embedder` returns `hash_embed` — a 64-dim
md5-bucket bag of words (`tools/vectorstore.py:66-74`, `:206-207`). Measured:

| Query fingerprint | Rows admitted | Cosine range |
|---|---|---|
| `kind:dataset dir:max accuracy fold classification tabular imputation` | 16 (the cap) | 0.522 – **0.818** |
| `kind:repo dir:max rerank russian retrieval ndcg` | 16 (the cap) | 0.527 – 0.620 |
| **`kind:banana dir:max zebra xylophone quixotic`** | **16 (the cap)** | 0.520 – **0.681** |

**A deliberately nonsensical fingerprint scores higher than a real one, and every query saturates
the cap.** The channel is not ranking; it is emitting a constant.

Consequence, verbatim, for a hypothetical new LLM-finetune task (`kind:repo dir:min loss training
learning rate gpu`), taken from the shipped code path over the real store:

> Lessons from related runs (what did/didn't work): op 'draft' with params {'x': 3.0187, 'y':
> -8.5513} reached 57.02 [supported]; op 'merge' with params {'x': 2.9712, 'y': -1.4797} reached
> 0.2309 [supported]; changing x 0.7176->0.5257, y -2.6862->-2.3026 improved the metric by 0.2337 …

That is toy-quadratic parameter noise injected into a GPU training run's prompt under the header
"lessons from related runs". It is not merely useless — it asserts that `x = 3.0187` is a good value
for a task that has no `x`.

*Remedies, in ascending cost — all product calls, so none was taken:* (a) skip the harmonic splice
when the embedder is the lexical fallback; (b) require a real embedding endpoint before `memora`
counts as on; (c) require a non-trivial fingerprint overlap on a harmonic hit as well.
Setting `memora=false` today makes the answer an honest **0 of 154** rather than five wrong rows.

### R2 — The one mechanism for cross-task transfer is disabled on exactly the runs worth transferring from.

The M2 fingerprint is what makes a lesson reachable from a *different* task. Measured length across
the run corpus (`reflection_note.fingerprint`):

| Run | Task | Fingerprint tokens |
|---|---|---|
| `live-cards-0804` | toy_quadratic | 5 |
| `live-deps4-0804` | deps_probe | 13 |
| `mnist-experiments` | mnist | 26 |
| `rubert-dr-0805` | rubert_dr_0804 | **71** |
| `rubertlite-dense-retrieval` | repo_task | **180**, **185** |

Jaccard ≥ 0.34 against a 180-token fingerprint requires **≥ 61 shared tokens**. No real task will ever
clear that. The long fingerprints are the goal text tokenised wholesale — `rubertlite`'s begins
`001, 100, 14400s, 20e, 58m, 8192, 859, above, absolute, accept, accumulate, after, all, allowed,
already, always, anneal, any, …`. So the richer the task, the more permanently unretrievable its
lessons become, and the toy runs — whose 5-token fingerprints match everything — dominate.

This is the structural reason the store is 88% toy even before the retrieval bug in R1.

### R3 — A run that reflects successfully has no receipt that its lessons reached the store.

`runs/rubertlite-dense-retrieval` appended `reflection_note {n_lessons: 13}` and
`{n_lessons: 11}` and 22 `lessons_distilled` events. **Zero rows with `task_id: repo_task` exist in
either memory dir.** (The historical cause — the tmpfs memory-dir loss — is fixed; the *reporting*
gap is not.)

`engine/lessons.py:149-156` appends `EV_LESSONS_STORE_UNAVAILABLE` on `OSError` only. Every other way
a batch can fail to land — a `memory_dir` pointing somewhere else, a store replaced underneath, the
whole tree wiped — is silent, and the run's own log keeps saying `n_lessons: 13` forever. Nothing
reconciles the two. There is no "N lessons distilled, M rows durable" check anywhere.

This also explains the split in the corpus: 46 runs wrote to `~/.looplab/memory` and 2 to
`/home/jovyan/data/looplab-memory`. The store the UI reads is a promoted copy, and nothing in the
product records which store a run wrote to.

### R4 — A crash-only run leaves nothing in two of the three tiers, and nothing at all offline.

Measured by running the shipped engine with an always-failing sandbox (4 nodes, 4 crashes, 0 evaluated):

| Configuration | lessons | meta-notes | cases | capsule |
|---|---|---|---|---|
| no LLM client (offline/toy) | **0** | 0 | 0 | 0 |
| LLM client wired | **2** ✅ | 0 | 0 | 0 |

The 2026-08-05 fix to `reflect_lessons` (removing the `best is None` short-circuit) **works** — with
a model, the run's failures are distilled and written. Two residual holes:

1. **The meta-note is unconditionally gated on a winner** (`lessons_distill.py:73`). The Notes tier
   is documented as "one line per finished run" and injected verbatim into the next run of the *same*
   task; a run that finished having proved the environment is broken contributes nothing to it.
   `reflection_note.note` is `""`.
2. **Offline, the contract is unmet.** `reflect_lessons` returns `_winner_lesson()` when
   `client is None`, and `_winner_lesson` returns `[]` when there is no winner. The docstring says
   *"a run in which every node failed is exactly the negative lesson M3 exists to record"* — offline,
   it records nothing, not even "4 nodes failed with reason crash".

Consequence: `/api/memory?run_id=<a crash-only run>` returns empty for all three tiers, which reads
as "this run taught us nothing" rather than "this run's teaching was discarded".

### R5 — A paused run never finalizes, so its negative lesson is never written at all.

`orchestrator.py:1368-1371` — `if state.paused: … break` — exits the loop **without finishing**, so
`finalize.py:647` (`if should_finalize and completed.finished …`) skips the entire finalize block,
reflection included. The developer-crash circuit breaker pauses on the **first** crash
(`orchestrator.py:4634-4645`), and its own comment states the intent: *"Freeze (not finish) so a plain
`resume` continues once the cause is resolved — no premature report/lessons."*

That is a defensible design. Its measured cost on this corpus:

| Run | Nodes | Failed | Events | Finished? | Memory written |
|---|---|---|---|---|---|
| `rubert-dr-0804` | 1 | 1 | 2 678 | **no** | nothing |
| `rubert-dr-0807` | 3 | 2 | 715 | **no** | nothing |
| `rubertlite-dr-unified-v4` | 11 | 10 | 4 856 | **no** | nothing |

Three GPU sessions, 8 249 events, zero durable knowledge. Nothing anywhere surfaces "this paused run
is holding an undistilled negative lesson" — not the attention feed, not the run list, not
`/api/memory`. Since the failure mode most likely to produce an all-failed run (a dead or
misconfigured provider) is *precisely* the one that trips this breaker, the negative-lesson machinery
is systematically starved of its best input.

### R6 — The paid concept steward has no consumer under shipped defaults.

- `cross_run_curation = True` (`config.py:984`) → every finalize with a model buys a steward
  proposal.
- `concept_tidy = False` (`config.py:997`) → nothing applies it.
- Measured: `concept_curation_log.jsonl` holds 4 rows, 2 of them real merge proposals
  (`experiment/validation/empirical → validation/empirical_confirmation`), every one with
  `receipt: null`. `concept_aliases.jsonl` **does not exist**. Zero aliases, splits or purges have
  ever been applied.

`engine/concept_tidy.py`'s own docstring names this: *"leaving a paid proposer with no consumer"*.
The module that closes it landed 2026-08-06 and is off by default, so today the gap is still open —
correctly, since ratification is the only path where an agent decision changes the taxonomy without a
human in the moment. But the *proposer* is on, so the operator pays for judgements nobody can act on
unless they know to run `looplab concept-ratify`.

Compounding it: the governance registry is consulted on the **read** path only. No tag writer
(`concept_cadence.py:296-320`, `lessons.py:448-453`) resolves an alias, so the store keeps minting
drifting spellings forever, and `ui/src/PortfolioConcepts.jsx:286-297` documents that nothing in the
browser reads `/api/cross-run/concept-policy` — so a merge the operator *does* apply changes nothing
on screen.

### R7 — The concept cadence fires once per short run, at node 3.

`concept_retag_every = 30`; `max_nodes = 8`. `_should_consult_concepts`
(`engine/concept_cadence.py:56-73`) returns True when `n == n_seeds` (3), then falls to
`cadence_due(n, last=3, every=30)`, which is false until n = 33.

**On the shipped defaults the classifier tags nodes 1–3 and never runs again.** Nodes 4–8 of every
default run are never classifier-tagged. That is the mechanism behind 3 capsules against 46 runs:
`lessons.py:504-508` refuses to write a capsule with no classifier evidence, and short runs mostly
have none. The 15 "tagged" runs the UI counts include *authored* tags, which are deliberately
excluded from the capsule's evidence channel — so the 46/15/3 gap is two different populations, not a
loss.

### R8 — Every finalize curation invocation on this corpus bought nothing.

| Ledger | Rows | `unavailable` | real outcome |
|---|---|---|---|
| `claim_curation_log.jsonl` | 189 | **185** | 3 proposed, 1 error |
| `task_facets_curation_log.jsonl` | 182 | **178** | 3 proposed, 1 error |
| `concept_curation_log.jsonl` | 4 | 0 | 2 proposed, 1 empty, 1 not-replayed |

`auto: false` and `auto_requested: false` on **all 375 rows**. `.curation_invocations/` holds **148
lock files and 11 receipts**. So the at-most-once paid-curation transaction ran ~375 times, recorded
`model: unknown` in 363 of them, and produced 8 proposals, none applied. On this corpus it is pure
finalize latency plus 330 KB of ledger.

### R9 — `evidence_count` claims more corroborating runs than the store has runs. *(fixed, see §4)*

| `evidence_count` | Statement |
|---|---|
| **55** | `changing x 0.2255->0.4047, y -1.9013->-2.7324 regressed the metric by 1.227` |
| **49** | `changing x 0.2255->-1.0835, y -1.9013->-1.7074 regressed the metric by 8.665` |

`lessons.jsonl` contains **46 distinct `run_id`s in total.** The UI renders this number as how many
experiments back a claim. `lesson_rank_key` clamps it at 3, so the ranking damage is bounded and the
*claim* is the damage.

One contributing path is fixed in this change (§4). The rest is structural: a consolidated row
carries only its base row's `run_id`, so a run that already contributed via an earlier fold is not in
`seen_runs` and can contribute again on a later pass. Closing that properly needs a durable
contributing-run set on the row — a schema change, and the operator's call.

### R10 — Auto-distilled skills are 50 files of toy operator text.

`~/.looplab/memory/skills/` holds `auto-mean-merge-of-nodes-0-1.md`,
`auto-perturb-best-node-0-metric-53-054167369999995.md`, and 48 siblings. Every agent with
`memory_dir` set gets `list_skills`, which shows the model all 50 names and descriptions. The
"skills" are the mechanical operator labels of toy runs.

`write_auto_skill` stamps `status: candidate | promoted` (`engine/memory.py:313-334`) and
`SkillLibrary._parse_skill` (`tools/skills.py:22-35`) reads only `name` and `description` — **a
candidate is offered to the model identically to a promoted one.** The promotion machinery is
inert. (`docs/guide/memory.md:169-172` already discloses this.)

### R11 — Cases: the tier nothing reads back, and the nine rows are thin.

A case is written once per finished run with a winner, upserted **by `task_id` with
retain-on-improvement** — so 9 tasks give exactly 9 rows. It is a per-task champion leaderboard.

The only path by which a case reaches an agent: `agents/factory.py:139-150` passes `cases.jsonl` into
`KnowledgeTools`, which renders each row as `PAST CASE — task …` into the **same** `kb` vector index
as the knowledge `.md` files, top-3, 600-char snippets. The model sees one only if it calls
`kb_search`, and **nothing in any prompt names `kb_search`**. `JsonlCaseLibrary.search()` and
`.all()` have zero production callers.

On the real corpus that path is close to vacuous: the knowledge dir is **empty**, so the `kb` index
is 9 cases; 5 of the 9 have a `rationale` of 22–23 characters; and all 9 predate the `run_id` field,
so `/api/memory?run_id=X` returns **zero cases for every run** and the concept shelf can never
inherit run-level tags for them.

*What a case is FOR, since the question was asked:* it is the reproducible winning configuration —
params + metric + the rationale that proposed them — kept per task so a rerun starts from the best
known point. That is a real purpose. Nothing currently uses it that way: no warm-start reads it, no
prompt injects it, and the one retrieval path drops objective direction (`knowledge_tools.py:299-302`
flags this itself), so a `min` case and a `max` case with the same goal rank identically.

### R12 — Writes with no reader.

| Written by | File / field | Read by |
|---|---|---|
| `concept_tidy.py::_append_ratification_receipt` | `concept_ratification_log.jsonl` | **nothing** (self-declared at `concept_tidy.py:463`) |
| `serve/routers/cross_run.py:644` | `/api/cross-run/concept-policy` | **nothing in `ui/`** (`PortfolioConcepts.jsx:286-297`) |
| `engine/memory.py:313-334` | auto-skill `status:` frontmatter | **nothing** (`tools/skills.py:22-35`) |
| `engine/novelty.py:998` | the `cross_run_prior` event | UI/audit only — *"NEVER changes the selection decision"* (`novelty.py:928-935`) |

The last one is worth flagging as a naming hazard rather than a defect: `cross_run_prior` sounds like
the thing injected into prompts and is not. The injected prior is
`lessons_priors.py::load_reflection_priors`.

---

## 3. Where two subsystems disagree about the same fact

1. **Direction.** The agent-facing `CrossRunTools` fails closed on a missing `direction`
   (`trust/cross_run.py:136-137`). **112 of 154 lesson rows have no `direction`** — invisible to the
   tools, and freely injected into the Researcher prompt by `lessons_priors`, which applies no
   direction check at all. Same store, two incompatible admission rules.
2. **Concept id spelling.** The shelf and the per-run tree use `normalize_concept_id`
   (spaces → `-`); `concept_capsules.jsonl` uses `normalize_key` (spaces preserved). A concept named
   `hard negative mining` is `hard-negative-mining` in one and `hard negative mining` in the other,
   and no reader can join them. Both spellings are deliberate and documented against each other; the
   *consequence* — a concept visible in the Memory panel that cross-run priors cannot see — is not
   surfaced anywhere.
3. **`evidence_count` vs the run count** (R9).
4. **`reflection_note.n_lessons` vs the store** (R3).

## 4. Docs vs code (only what outlives a run; the ten known gaps excluded)

| # | Doc | Promise | Code | Cost |
|---|---|---|---|---|
| D1 | `configuration.md:612` | `looplab governance concept-ratify [--dry-run]` | `cli/governance_cmds.py:402` registers it **flat**; there is no `governance` sub-app. Real form: `looplab concept-ratify MEMORY_DIR`. `cli-reference.md:944` is right; `concept_tidy.py:105` repeats the wrong spelling | the documented dry-run — the only safe preview of the one unattended taxonomy write — exits 2 |
| D2 | `02-architecture.md:250`, `03-decisions.md:370`, `01-product-design.md:200` | "tiered promotion `candidate→distilled→trusted`; **decaying confidence**; contradiction = mark-invalid + append-only ledger" | confidence is a constant `0.6` (`lessons_distill.py:328`); `grep -rn "decay" looplab/` finds only "weight decay"; there is no lesson `status` field; superseded rows are **physically removed**, not marked invalid | the docs promise memory that ages out; a lesson keeps full weight forever until directly contradicted |
| D3 | `02-architecture.md:249`, `03-decisions.md:369` | "**Curated knowledge is never merged with** distractor-rich ingested RAG"; a "role/goal-conditioned router" | `knowledge_tools.py:271-315` puts knowledge notes **and** `cases.jsonl` into one index literally named `"kb"`; there is no router; k=3 is one shared budget | opposite answers to "does a past case compete with my curated notes for a retrieval slot". `guide/memory.md:78-79` is the correct one |
| D4 | `02-architecture.md:597`, `:580` | "pluggable `VectorStore` (**LanceDB** default …) + optional `[[wikilinks]]→networkx` graph", swappable via `config.knowledge.index.backend` | one implementation, `InMemoryVectorStore`; zero hits for `lancedb\|qdrant\|faiss\|chroma`; no wikilink parsing; `Settings` is flat by contract so that config path cannot exist | "swap the vector DB" is not an operation; the real cost model is the opposite — the index is in-process and re-embedded on every build |
| D5 | `02-architecture.md:251`, `03-decisions.md:375-380` | `knowledge/{seed,tasks,experiments,lessons}/*.md` + derived `knowledge/index/` | `knowledge_dir` is a flat `*.md` tree; lessons live in a different directory and format (`<memory_dir>/lessons.jsonl`); `lessons/<topic>.md` is never created | anyone who lays out `knowledge/` per the doc gets a tree the engine ignores |
| D6 | `guide/memory.md:391` | run-end order "case/claims/capsule → reflection → concept steward → claim steward → task facets → llm_cost → completion" | omits card enrichment (`finalize.py:750`) and **concept ratification** (`:820`) | this is the page's authority for reasoning about a crash mid-finalize, and it hides the one stage that performs a governance write |
| D7 | `guide/concepts.md` | 950 lines documenting merge/split/purge/clear/CAS | **zero** mentions of `concept_tidy` / ratification | the concept guide still says taxonomy changes only by human action |
| D8 | `guide/memory.md:22` | "Research Atlas \| Runs → Atlas preview \| … rolled up over **every** run in the memory dir" | `#/atlas` is top-level, not under Runs; its concept half reads the **3-row capsule ledger** | a thin Atlas reads as "the portfolio never tried this" |
| D9 | `guide/memory.md:18-46` | "Lab → Authoring", "Lab → Memory" | shipped labels are "Cross-run memory" and "Knowledge & prompts" | the page that answers "where do I edit a prompt vs read a lesson" names two menus that don't exist |
| D10 | `configuration.md:603` | `cross_run_read_tools` adds 5 named tools | it registers **8** — also `similar_runs`, `find_concept_slugs`, `concept_card` | disabling the flag silently costs `find_concept_slugs`, the thing that stops every run minting a near-duplicate slug |
| D11 | `guide/memory.md:29` | knowledge is "same directory, same files" on both panels | the agent index globs **recursively** (`tools/retrieval.py:74-79`); the authoring API lists `root.glob("*.md")` only | `knowledge/sub/foo.md` is retrievable by the Researcher and invisible in the panel |

## 5. What is fine

One line each, because a list of only problems is not trustworthy.

- **The memory-panel blurbs are already there, and they are more accurate than the docs.**
  `ui/src/panels.jsx:1812-1836` gives each of lessons / cases / notes / knowledge a "what this is"
  paragraph, and the cases one says the honest thing verbatim: *"**Not pasted into any prompt**: an
  agent sees a case only if it calls `kb_search`."* The "bare labels" complaint is fixed.
- **Prompt-slot rationing works.** 131 exact-task meta-notes collapse to 3 slots and all three shown
  are genuinely distinct; `prompt_slot_key` folds 154 lesson rows into 47 families and every row it
  folds is a templated fallback.
- **`consolidate_lessons`'s identity key is right to keep digits** — those 105 rows are distinct
  measurements, and merging them would destroy evidence.
- **`filter_contradicted` + the §role-split are correct**: a Researcher lesson and a Developer lesson
  never merge, and the read path filters by role before contradiction-checking, so no role's verdict
  can be retired by the other's.
- **`JsonlCaseLibrary` is careful**: re-reads inside the interprocess lock, refuses to let an
  unmeasured case displace a measured one, and preserves quarantined bytes across every rewrite.
- **The finalize ladder discloses its own skips** — a run with reflection off emits
  `finalize_step {step: reflection, outcome: "disabled"}` rather than going quiet.
- **`configuration.md`'s defaults are correct.** All 185 rows were diffed against resolved
  `Settings` defaults; every memory/concept/lesson/curation field is right. The only absent rows are
  9 per-role LLM fields.
- **`guide/concepts.md:819` already discloses the 15-tagged-runs / 3-capsules gap**, and
  `:679-680` correctly says retro-tagging never rewrites an emitted capsule.
- **The process diagram is accurate on this subject and already shows the ratification stage**
  (`agent-architecture.html:136`, `:153`), with every cadence and threshold matching code.
- **`co_occurs` is derived, never persisted** — the fold explicitly drops legacy rows
  (`replay.py:2366-2371`) because a ratcheting max ledger leaves ghost edges. Correct call.
- **The crash-only lesson fix from 2026-08-05 works**, verified by running it (§R4).
- **Memory tier reads are bounded and receipted**: 2 MiB / 1000-row window, 200-row cap, and
  `source_window_truncated` distinguishes "not in the recent tail" from "does not exist".
- **Compaction is inert and correctly so**: `compact_lessons(max_lines=4000, keep=2000)` cannot fire
  at 154 rows, so nothing is being silently dropped from the oldest prefix today.

## 6. The change in this commit

`engine/lesson_hygiene.py` had two per-group rules that must agree, and only one was shared.
`_verdict_base` was hoisted precisely so the exact-key pass and the agent paraphrase pass "*can never
drift apart*" on which row carries the verdict — but the *evidence* rule stayed inline in the exact
pass, and the paraphrase pass summed member `evidence_count` raw:

```python
row["evidence_count"] = sum(int(rows[m].get("evidence_count", 1) or 1) for m in members
                            if rows[m].get("outcome") == base.get("outcome"))
```

No `run_id` dedup — while the exact pass three functions above has one, with a comment explaining
that a run re-reflecting itself "*must count ONCE, not inflate the count*".

This is the paraphrase pass's own population, not an edge case: it exists to merge rows the exact key
**misses**, and one run's two wordings of one finding (the mid-run `lessons_every` cadence and the
run-end reflection) are exactly such a pair. Raw-summed, one run's single finding claimed two runs'
corroboration.

The rule is now `_accumulated_evidence`, stated once beside `_verdict_base` and applied by both
passes. `tests/test_phase3_memory.py::test_agentic_merge_dedups_evidence_by_run_id` drives it, and is
red on a revert of the production line alone (`assert 2 == 1`).

This closes one contributing path to R9. It does not close R9 — see the note there.

## 7. If only three things get acted on

1. **R1** — the harmonic channel is injecting noise into every prompt on every task today. It is one
   `if` away from being honest, and the fix costs nothing but content nobody wanted.
2. **R5 + R3** — a paused or crashed run should leave *something* durable, and a run should be able
   to prove its lessons landed. Between them they account for every GPU session in this corpus
   contributing zero.
3. **R6** — either turn `concept_tidy` on or stop buying steward proposals; paying for judgements
   with no consumer is the worst of both.
