# Memory & knowledge

LoopLab remembers. Across a run and across runs it accumulates several **distinct kinds of memory**,
each with its own purpose, and it both **injects** the relevant ones into the proposal prompt *and*
lets the agent **actively pull** any of them on demand. This page is the structured reference: the
types, what each is *for*, how it's written and read, and the methodologies that move data through it.

Both stores are **on by default** (a user asked for it): `~/.looplab/memory` (cross-run memory) and
`~/.looplab/knowledge` (the knowledge base). Relocate with `LOOPLAB_MEMORY_DIR` /
`LOOPLAB_KNOWLEDGE_DIR`, or set either to `""` to disable. See [Configuration](configuration.md).

## Which surface am I looking at?

There are **three** UI surfaces over durable knowledge, and the thing that separates them is *who
writes*, not *what the content is about*. That is the whole answer to "what is the difference
between Authoring and Memory".

| Surface | Where | What it holds | Who writes it | Editable there? |
|---|---|---|---|---|
| **Authoring** | Lab → Authoring | `prompts`, `skills`, `knowledge` — three directories of Markdown | **You** (and, for `knowledge`, the assistant's `remember` tool) | yes |
| **Memory** | Lab → Memory | Lessons, Cases, Notes (+ a read-only view of the same `knowledge` notes) | **The runs**, at run end | no |
| **Research Atlas** | Runs → Atlas preview | Concepts and claims rolled up over *every* run in the memory dir | Derived at read time from what the runs wrote, plus your governance decisions | no (governance is CLI/HTTP) |

Three consequences worth stating plainly, because each of them has been read as a bug:

- **Skills and prompts are not missing from Memory — they are on the Authoring panel**, because a
  human writes them. They are just as durable as a lesson.
- **`knowledge` appears on both panels.** It is the one kind both you and the agents write, so
  Authoring shows it writable and Memory shows it read-only. Same directory, same files.
- **A claim in the Atlas will not appear in Memory under that name.** Claims are not a fourth store:
  `engine/claims.py` groups the *same* `lessons.jsonl` rows by normalized statement and joins them
  with the deep-research memo claims in `research_claims.jsonl`. The Atlas is a view, not a tier.

## The types (what each is *for*)

Each type is deliberately different — they are **not** interchangeable:

| Type | Essence — what it's for | Stores | Scope | Written | Read |
|---|---|---|---|---|---|
| **Cases** (`cases.jsonl`) | *The list of the best* — the winning config per task, verbatim, retain-on-improvement. Exactly one row per `task_id`: a leaderboard, not a history. | `{task_id, goal, direction, params, metric, rationale}` | cross-run | run-end | **`kb_search` only** — a case is never injected into a prompt (see [What are cases for?](#what-are-cases-for)) |
| **Meta-notes** (`meta_notes.jsonl`) | *Why it may have won* — a short, LLM-distilled explanatory hypothesis over the observed run (not the raw config — that's the case, and not causal proof). | `{task_id, note}` (model-authored explanatory prose) | cross-run (per task) | run-end (LLM; falls back to a stats line) | exact warm-start, `recall_notes` |
| **Lessons** (`lessons.jsonl`) | *Generalizable good **and** bad findings* — higher-level claims ("larger batch tends to help") with a verdict and a count of agreeing recorded observations, not independent verification. **Split by `role`** (see below). | `{statement, outcome: supported/tested/abandoned/failed/refuted/noted (action guidance; noted is neutral), claim_stance: support/oppose/neutral (relation of evidence to the literal statement on new rows), delta, confidence, evidence, evidence_sig (each evidence node's outcome signature at write time — the reconciliation provenance), evidence_count, fingerprint, role: researcher/developer (absent = shared)}` | cross-run (task-fingerprint matched) | run-end — **LLM-authored only**: the reflection consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one lesson per theme, plus M6 comparative code-fix pairs (offline/toy path: a deterministic winner record); also **re-derived when a re-eval flips a cited node** | prompt injection (role-routed, fingerprint-matched), `search_lessons` |
| **Skills** — hand-written (`skills_dir`) | *Best practices **with the script*** — a reusable technique + the code that implemented it, offered to the Researcher as a tool. | markdown: `name`, `description` frontmatter + the technique in the body | cross-run | **by you**, in Lab → Authoring → skills | `list_skills`, `use_skill` |
| **Skills** — auto-distilled (`<memory_dir>/skills/auto-*.md`) | The same tool surface, filled by the run itself from a card that was supported with Δ>0. | as above, plus `status (candidate/promoted)`, `provenance`, `source_task`, `fingerprints` — **none of which any reader parses** (`tools/skills.py::_parse_skill` reads `name`/`description` only) | cross-run | run-end (supported hypothesis, Δ>0) | `list_skills`, `use_skill` — a *candidate* is offered exactly like a *promoted* one |
| **Knowledge base** (`knowledge/*.md`) | *Anything worth keeping* — free-form notes, hand- or agent-authored (the assistant's `remember` tool). The one kind **both** you and the agents write. | markdown notes | cross-run | assistant `remember`, or Lab → Authoring → knowledge | `kb_search`, `list_notes`, `read_note` |
| **Prompts** (`<prompt_dir>/<key>.md`) | *What the roles are told* — an override that REPLACES a built-in role system prompt, re-read on every call. Not learned and never written by a run: operator configuration that happens to live on disk. | one Markdown body per key in `core/prompts.py::PROMPT_KEYS` | global (a flat `Settings` field) | **by you**, in Lab → Authoring → prompts | every LLM call for that role, via `render(prompts, key, default)` |
| **Cards** (belief board, in-run; 1 card = 1 hypothesis) | *What's worth testing* — accepted "to-test" beliefs with a live **verdict** (open → testing → supported/tested/abandoned) and accumulating evidence. Deduped by exact hash **plus** an agentic paraphrase-merge (`hypothesis_merged` — the engine decides, the fold applies it deterministically, cadence open≥4 & grew≥2); the open board is prioritized by **foresight**. | `{seed_statement, verdict, evidence, best_delta, source}` per card | one run (derived from the event log) | Researcher (`idea.hypothesis`) + `hypothesis_added` | injected into the proposal prompt |
| **Research memo** (deep-research) | *Breadth of scope* — a hard-thinking pass over all results plus enabled sources; its `recommended_directions` can **become belief cards**. | `{summary, reasoning, findings, claims:[{statement, node_ids, urls}], sources:[{title,url,snippet}], recommended_directions, proposed_ideas, at_node, trigger, verification}` (`reasoning` is debug-only; `verification` is the persisted D8 verdict payload when available) | one run | on cadence / manual / strategist | folded into `RunState.research`, verified by the D8 verifier |
| **Exploits** (`exploits.jsonl`) | *Defensive memory* — patterns of cheating/leakage the trust layer scans for (co-evolved hacker-fixer). | `{name, pattern, kind}` | cross-run | `looplab harden` (CLI only — no UI) | reward-hack scan at eval. **The only cross-run store that can change a node's fate**, via `trust_gate` |
| **Concept capsules** (`concept_capsules.jsonl`) | *What this run tried, as concept slugs* — one capsule per run, with a per-concept outcome sign relative to that run's own field. The unit the Atlas aggregates. | v2 record: `{v, run_id, task_id, fingerprint, direction, concepts, concept_outcomes, concept_signs, best_metric}` + source-completeness receipts | cross-run | run-end, gated on `cross_run_concepts` | Atlas, the Researcher context pack, the Strategist note, seven `cross_run_*` tools, and `grade_novelty(prior_concepts=…)` — **surfaced, never a rejection** |
| **Research claims** (`research_claims.jsonl`) | *What a deep-research memo asserted, with its citations* — the counter-evidence half of a claim: the only source that can make a claim **contested**. | v3 record: `{v, record_kind, run_id, task_id, direction, statement, metric, node_ids, urls, verification, source_receipt}` | cross-run (exact task) | run-end, from the D8 memo ledger | claim/atlas projections → context pack, Strategist note, `cross_run_claims`/`atlas`/`search`, `/api/cross-run/claims` |
| **Governance policy** (`claim_decisions.jsonl`, `concept_aliases.jsonl`, `concept_splits.jsonl`) | *Your* corrections to the portfolio — ratify/reject/pin a claim; merge, purge or split a concept slug. Applied at READ time, so nothing is destroyed. | one action row each, with `action_id` + revision CAS | cross-run | **you** — `looplab claim-decide` / `concept-merge` / `concept-split`, or the `/api/cross-run/*` routes — plus, for concept MERGES only and only when `concept_tidy` is on, the ratification stage applying what the steward already proposed (`by=concept-ratifier/v1`, undone by the same `concept-alias-clear` you would use on your own row) | overlays every claim and concept projection an agent sees |
| **Steward proposals** (`*_curation_log.jsonl`) | *What a paid steward suggested* — a review queue plus the idempotency ledger that stops the same paid call being charged twice. | `{v:2, curation_key, input_digest, model, outcome, proposals, …}` | cross-run | run-end stewards (`cross_run_curation`) or the CLI/HTTP steward commands | **nothing reads these into a prompt or a decision.** They are an audit trail and a human queue: a proposal changes nothing until you issue the governance write above |
| **Task facets** (`task_facets.jsonl`) | *What KIND of problem this task is* (domain / language / modality / interaction / objective), classified by an LLM. | `{task_id, facets:{axis: value}, by, at}` | cross-run (per task) | `looplab task-facets-set` (CLI only) | **nothing that changes behaviour** — `scope_profile` accepts them but the deterministic index path never passes them, so they neither grant visibility nor change ordering (`engine/task_facets.py` says so itself) |

In-run **working memory** (rebuilt from the event log each turn, never persisted separately): the
**belief-card board**, the **diversity archive** (MAP-Elites elites/niches), and the **digests** —
`experiments_digest` (winners + failures + sweep landscape), `sibling_digest` (what siblings already
tried), `lineage_lessons` (subtree outcomes ranked by |Δ|), `ancestral_repair_chain` (prior repairs).
The append-only `events.jsonl` is authoritative for the replayable `RunState`. Task/config snapshots,
diagnostic spans, chat, command records and cross-run stores retain separate documented sidecar contracts;
`replay.fold` does not manufacture them.

## Where the taxonomy is thin — read this before adding a kind

The table above is what each kind is *designed* for. This section is what the code actually does,
including the places where two kinds turn out to be near-duplicates and the places where a kind is
written but never consumed. Nothing here is proposed for deletion — a kind that is inert today may
be a deliberate placeholder — but each of these has cost somebody an afternoon.

### What are cases for?

Honestly: **less than the name suggests.** A case is the only place the winning **params** survive a
run in structured form, and that is a real property. But trace the readers and the list is short:

* `agents/factory.py` passes `cases.jsonl` into `KnowledgeTools`, which renders each row as a
  `PAST CASE — task …` block and embeds it into the **same** `kb` index as the knowledge notes. So a
  case reaches a model only when a tool-using role chooses to call `kb_search`, and then only if it
  wins a top-3 semantic ranking against every knowledge note.
* `GET /api/memory` renders them on the Memory panel.
* `JsonlCaseLibrary._reload` reads them back so `add` can keep the better metric.

### `GET /api/memory` and what it can honestly say about one experiment

`GET /api/memory` reads a **bounded recent tail** of each of the three tiers (last 2 MiB / 1000 lines
of the file, 200 rows returned), and each tier ships a receipt — `limit`, `returned`, `skipped`
(unreadable rows), `filtered` (excluded by the run filter below), `source_window_truncated`,
`unavailable`. `?run_id=` narrows all three tiers to rows naming that run; it is applied **inside the
source-window scan**, before the per-tier cap, so a busy store cannot report that a run contributed
nothing merely because newer rows sit in front of it. It is a filter over the same window, never a
wider read — an old run can still fall outside it entirely, which is what `source_window_truncated`
says. Omitting the parameter is byte-for-byte the whole-store projection the Memory panel reads.

A **lesson** row is published with the node provenance it actually carries: `evidence` (the credited
node ids) and `evidence_generations` (each id's node *attempt*, parsed from the durable
`evidence_sig`), beside the consolidation-written scalar `evidence_count`. `evidence_generations` is
kept separate from `evidence` and is **absent** rather than null-filled when a row records no
attempt, because "attempt unrecorded" and "attempt 0" are different facts.

What this can and cannot support is worth stating plainly, because the Inspector's *What this
experiment taught* section is built on it:

* the **run's event log** is the exact, immutable half — `lessons_distilled` (folded, and on
  `GET /api/runs/{id}/state`) carries `evidence_refs: [{node_id, generation}]` and a content-addressed
  `lesson_id`;
* the **cross-run store** is the mutable half, and consolidation is destructive: `consolidate_lessons`
  merges a group as `merged = dict(newest)`, so every non-base row's `run_id`, `evidence`,
  `evidence_sig`, `concepts` and `fingerprint` are dropped and only `evidence_count` accumulates; the
  agentic pass replaces the statement with LLM-written text; superseded rows are physically removed.
  There is **no `merged_from`, no tombstone and no redirect**, so a merged-away lesson cannot be
  resolved to its descendant — only reported as no longer present as written;
* **cases** carry no node id at all, and **meta-notes** carry no node id (and no `run_id` at all when
  written off the finalize path);
* run-end **reflect** lessons ride on the diagnostic `reflection_note` event, which carries neither
  `lesson_id` nor evidence, so they are attributable at run level only;
* **knowledge notes** (`LOOPLAB_KNOWLEDGE_DIR`) carry no provenance whatsoever and can never be
  attributed to a run or a node.

That is the complete list. No prompt injection, no warm start, no gate, no score, no selection.
`JsonlCaseLibrary.search()` and `.all()` have **no production call sites at all**. The
vector/Memora-capable `CaseLibrary` in the same module is explicitly unwired (`tests/test_case_store_wiring.py`
holds that line).

Meanwhile the **meta-note** for the same finished run says the same thing in prose —
`best metric 0.925 via op 'draft' params {'iters': 500.0, 'k_folds': 5.0}` — and *that* one **is**
injected, verbatim, into the next run of the same task. So on the exact-task path the case is the
structured twin of a note that already reaches the prompt, and on the similar-task path the case is
keyed by `task_id` and cannot transfer at all (lessons and capsules carry the goal fingerprint that
makes fuzzy transfer possible; a case does not).

**If the merge tempts you** — folding cases into meta-notes, or promoting the case into the prior the
way notes are — the cost is: (1) `cases.jsonl` is unversioned by contract (`valid_case_record`
quarantines any row carrying `v` or `record_kind`), so it cannot be migrated in place, only rewritten
under a new filename; (2) `KnowledgeTools` would lose its only non-note corpus and `kb_search`'s
behaviour would change for every existing memory dir; (3) the params are the one artifact a human can
paste into a re-run, and prose is not a substitute. This is the operator's call, not a refactor.

### Authoring vs Memory

The boundary is **direction of authorship**, and it is principled — but only one word on each panel
ever said so, and neither said it about the other. Authoring is the three directories a human fills
(`prompt_dir`, `skills_dir`, `knowledge_dir`); Memory is what a run appends when it finishes. Both
are durable, both are cross-run, both are read by the same agents.

Two things blur it, and both are real:

* **`knowledge` is on both panels** — writable in Authoring, read-only in Memory — because it is the
  one directory both parties write. That is not a bug, but nothing in the product said so.
* **Auto-distilled skills break the rule.** `write_auto_skill` puts run-authored skills in
  `<memory_dir>/skills/`, and `SkillTools` reads them alongside the hand-written ones. So there is
  machine-written content on the Authoring side of the line — and it is visible in **neither** panel:
  the Memory endpoint only serves `cases`/`lessons`/`meta_notes`, and `GET /api/skills` resolves
  `settings.skills_dir`, which is a different directory (and `None` by default).

### Skills and prompts

Both are real durable kinds and both are already in the product — on the **Authoring** panel, because
a human writes them. Three concrete gaps sit behind "why aren't they there":

1. **`skills_dir` and `prompt_dir` default to `None`.** Out of the box both Authoring tabs render
   `no skills dir configured` / `no prompts dir configured` — indistinguishable, to a new operator,
   from "this feature does not exist". `memory_dir` and `knowledge_dir` default to `~/.looplab/…`;
   these two do not.
2. **The Authoring list only globs root-level `*.md`.** `SkillTools` discovers `**/SKILL.md` *and*
   `*.md`; `GET /api/{kind}` lists `root.glob("*.md")`. A packaged `my-skill/SKILL.md` is therefore
   loadable by the Researcher and invisible in the editor — while the `skills_dir` settings help
   explicitly advertises "Recursive `*/SKILL.md` packages".
3. **Auto-distilled skills have no surface at all** (see above), and their promotion metadata is
   inert: `write_auto_skill` takes an interprocess lock to maintain `status: candidate|promoted` from
   accumulated task fingerprints, and `tools/skills.py::_parse_skill` reads only `name` and
   `description`. A candidate is offered to the model exactly like a promoted one, so today the
   candidate→promoted machinery buys nothing but the lock.

### Kinds nothing reads back

Written durably, at cost, and consumed by no prompt and no decision:

* **Task facets** — an LLM classification per task. The module's own docstring says they "do not
  currently change retrieval order"; the only reader of the content is the writer's own
  once-per-task dedup.
* **Steward curation logs** — by design: the stewards are proposal-only, and the log is the human
  review queue plus the paid-call idempotency ledger. Worth knowing it is not memory the agents read.
* **Auto-skill `status`/`provenance`/`fingerprints`/`source_task`** — see above.

## Current cross-run boundary and the Research Atlas target

The shipped memory above is useful, but it is not yet a complete scientific index over a large portfolio.
LoopLab also ships an **experimental Part-IV slice enabled by default in product `Settings`** (the
bare-library `EngineOptions` defaults remain off): rebuildable run passports/facts, per-run
concept capsules with alias/split overlays, v3 persisted D8 claims, task-facet overlays, bounded hybrid
cross-run retrieval, and backend Atlas/claims projections. Bound pull tools apply role and compatible direction;
capsule upsert identity is currently the display `run_id` alone. Because the default store is global,
two independent run roots that reuse a local run id can replace each other's capsule; there is not yet a
persisted portfolio-wide run-incarnation UID.
lessons/capsules accept exact task or a strict related-goal fingerprint, while v3 D8 (which stores no goal
fingerprint) is exact-task-only. Task facets are metadata reserved for future post-scope ranking and currently
neither grant visibility nor change ordering. External coding-agent Developer backends receive no D8 provider,
while the standalone CLI remains portfolio-wide. That difference is now DECLARED at provider
construction rather than inferred from whether run-binding happened: a model-facing provider is
built for one run, so if it is never bound it answers with an explicit "not bound to this run" and
no rows, instead of silently falling back to every run's. Proactive Researcher/Strategist influence persists lean
source/render digest receipts. Typed owner governance writes now have revision CAS, action-id idempotency and
explicit clear actions, while stewards remain proposal-only. These projections are real, but they do not yet
provide an immutable comparison/access scope, one portfolio-wide atomic snapshot, a complete concept/corpus
coverage denominator, evidence/taxonomy
release identity, assignment backfill or independent evidence-family accounting. Typed owner HTTP concept
actions now validate live canonical merge/purge sources and merge targets; split may introduce provisional
children, but this is not a versioned taxonomy/entity release. Typed
claim decisions do fence a current claim and its observed evidence digest. An owner-only `#/atlas`
**Experimental portfolio diagnostic** now renders the bounded read models. Its claim/evidence slices carry
coherent source identity, but the four independently fetched projections are not the complete canonical
Research Atlas. The home Runs Lineage view and a
run's theme grouping are different surfaces (see [Web UI](ui.md#which-graph-am-i-looking-at)).

Concept capsule v2 has additive bounded-source receipts for its applicability fingerprint and both stored
collections: `fingerprint_total` / `fingerprint_omitted` / `fingerprint_complete`,
`concepts_total` / `concepts_omitted` / `concepts_complete` and
`concept_outcomes_total` / `concept_outcomes_omitted` / `concept_outcomes_complete`. A new writer computes
within-run rank signs against the full valid outcome field before retaining the bounded projection. Invalid
concept IDs and outcome keys are never persisted as evidence and count as omitted input, so their removal cannot
produce `complete=true`; an invalid direction is rejected instead of being coerced into inverted `min` evidence.
An unusable label the capsule WRITER drops before the builder sees it — the only way it can key one concept
under one spelling — is counted on the producer denominator below instead, marking that node incomplete, so it
likewise cannot produce `complete=true`.
The writer also persists the classifier-producer denominator
`concept_evidence_nodes_total` / `concept_evidence_nodes_incomplete` / `concept_evidence_complete` over active
nodes. Tombstoned and aborted nodes are excluded. Valid labels retained from an incomplete classifier result
remain positive observations, but both collection `*_complete` flags and every downstream `source_complete`
receipt stay false. A partial-only run writes an empty lower-bound capsule instead of disappearing as an
apparently unobserved run. The three producer fields are atomic and strictly validated; an older v2 capsule
without them remains readable but has an unknown membership denominator and is treated as partial.
A capped fingerprint, or a legacy v2 row without its fingerprint receipt, remains usable for an exact
`task_id` but cannot authorize fuzzy related-task transfer. Bound tools and proactive context retain an
aggregate `scope_complete` / `scope_unknown_capsules` receipt for those excluded rows, so a filtered empty
result is reported as unknown rather than proof that no applicable run exists. A legacy v2 row without either concept triplet
remains readable for its positive retained
concept/outcome observations, but its source totals are **unknown**, the portfolio projection is partial, and its
old `concept_signs` are ignored because the former writer may have calculated them after truncation. Overview,
graph, digest, CLI and agent-facing context surfaces carry or render this partial-source receipt. The mutable
capsule file also has an additive read-health receipt (`source_store_complete`, `source_rows_total`, and
malformed/schema-invalid/duplicate quarantine counts). Quarantined content is never returned as evidence, but
any quarantined durable row forces `source_complete=false`; scope filtering and de-duplication preserve that
receipt so an unreadable row cannot be laundered into an exact zero or a "new concept" claim.
`partial_capsules` is deliberately orthogonal: it counts readable capsule rows with incomplete/unknown
per-capsule bounds, so it can be zero while file-level quarantine still makes `source_complete=false`.
Consumers must treat `source_complete` as the authority and must never infer completeness from
`partial_capsules == 0`.

An evidence ref is a node id — an index into that run's node table — so it is non-negative by
construction. A negative id (as an int or a signed numeric string) would run-qualify into a citation
like `run:-1`: an authoritative-looking pointer to a node that cannot exist. It quarantines its row
rather than being silently dropped, for the same reason as every other poisoned element — repairing
the row in place would leave the surrounding claim marked complete and trustworthy.

D8 claim v3 repeats a validated per-run producer receipt on every retained row (or writes a non-indexed
receipt sentinel when a non-empty source retains zero claims):
`claims_total`, `claims_retained`, `claims_omitted`, and `producer_complete`. The writer scans for the first
256 valid claims instead of slicing the raw memo first, so malformed prefix entries cannot hide a valid later
claim. Invalid and capped inputs both count as omitted. Claim projections aggregate those receipts as
`research_source`; a v1/v2/unversioned durable row has an unknown denominator and fails closed. The current
additive `read_health_v=1` extension also carries `read_complete`, durable row
total/retained/quarantined counts, malformed/invalid counts and a lowercase snapshot digest. The extension is
atomic; a legacy producer-only outward receipt remains readable, but a partially present or contradictory
extension is invalid. These producer/read-health fields describe the D8 rows that were explicitly processed
and persisted, not proof that every portfolio run executed D8.

A positive D8 verifier verdict is promotable only when every retained citation was inspected: node references
must name terminal, active attempts and every cited URL identity must match a source actually consulted by the
research stage. Finalization reconstructs the complete unique node/URL identity set from the durable claim and
requires exact equality with the verifier receipt; a subset receipt, pending attempt, reset, tombstone or abort
downgrades the claim to unverified evidence rather than durable support.

Exact claim authority is the separate v1 `claim_source`. It joins the lesson and research read-health
segments with D8 producer completeness and binds the combined snapshot with a digest. Retained evidence
remains visible and citable, but a quarantined lesson/research row, a partial/unknown D8 source or an unknown
combined receipt cannot produce either exact one-sided state (`supported`/`refuted`) because omitted evidence
may make it mixed. It also cannot produce an agentic `ratified` proposal. Context packs, retrieval receipts,
the claims endpoint/CLI, and the Atlas preview disclose the lower bound. The producer-prefixed receipt remains
additive: read health refines overall D8 completeness without redefining what the producer-cap fields mean.

### Operator-governance ledger health

`concept_aliases.jsonl`, `concept_splits.jsonl`, and `claim_decisions.jsonl` are policy, not
best-effort memory. The `concept_curation_log.jsonl`, `claim_curation_log.jsonl`, and
`task_facets_curation_log.jsonl` sidecars are also authority for paid steward idempotency: skipping a
durable begin/outcome could charge the same concept, claim, or task-facets decision again. A skipped
row could otherwise be a merge, purge, split, clear, rejection, or pin and would
change canonical identity or which claims reach a live run. Readers therefore require every physical
row to be newline-terminated JSON object data with a known schema/action, valid bounded fields, unique
`action_id`, and consistent writer-owned revisions. Invalid JSON, a non-object row, torn tail,
unknown/future action or schema, duplicate/colliding action IDs, and revision collisions make that
ledger **unavailable**; the valid prefix is not applied as though it were complete.

That health state propagates through overview, retrieval, Atlas, curation, agent tools, CLI, and owner
HTTP reads. HTTP returns a versioned (`v: 1`) `503 governance_ledger_unavailable` no-store receipt
containing only the ledger and a closed reason class; poisoned row content and local paths are never
reflected. Healthy curation-history reads explicitly report `status: complete` and `complete: true`;
there is no partial-200 audit history. Normal
operator mutations also refuse to append while a ledger is unhealthy, so a later write cannot
silently bury the quarantine behind a new revision. There is intentionally no automatic semantic
repair: stop writers, preserve a byte-for-byte backup, and restore or explicitly repair the ledger
offline after identifying the damaged operator action. `looplab repair-log` is for run
`events.jsonl`, not these governance sidecars.

Revision-labelled Atlas, retrieval, and owner claim projections use one lock hierarchy:
concept-global policy, then claim decisions, then the participating evidence files in sorted path
order. The response payload is built before those locks are released, so its evidence and policy
revision are one snapshot rather than a hybrid of adjacent writes. Operator alias, split, and claim
decision writes also require confirmed file sync (plus first-create directory publication) before
success is acknowledged. A sync/capability failure returns the same content-free `503`/no-store health
boundary. An idempotent retry re-syncs the existing receipt before returning it; it does not append a
second revision merely because the first acknowledgement failed.

The broader Part-IV design specifies the production **cross-run research index** and UI **Research Atlas**.
Its core distinction is:

- a faceted applicability profile says **where** evidence may transfer (application, entities/modalities,
  domain, language, dataset lineage, objective/metric, constraints, codebase and environment);
- a versioned concept/technology graph says **what** was tried;
- immutable run events/attempt measurements say **what actually happened**; the current node outcome is a
  projection over generations;
- scoped claims say **what the evidence currently suggests**, including opposition, uncertainty and
  freshness;
- incremental run capsules and portfolio/concept summaries make 50–500 runs cheap to navigate, while every
  result remains drillable to the exact run/node evidence.

This is deliberately not one global vector store or one topic tree. Projects/super-tasks remain user
organization; task applicability and technology concepts are orthogonal. A cross-run novelty hit surfaces
prior outcomes and their conditions; it does not automatically reject an adjacent-domain idea. The target
schema, retrieval/context contract, UI, lifecycle corners, alternatives and CR0–CR3 rollout are in
[Project review §21.20](../17-project-review-and-directions-2026-07-11.md#cross-run-research-architecture).

### Finalize steward identity and ordering

Finalize stewards are proposal producers, not governance writers. Their paid-work identity is semantic and
independent of whichever run happened to trigger finalize:

- concept and claim curation freeze the exact bounded model-visible payload, include a versioned
  `input_schema`, and use its canonical SHA-256 `input_digest` as the `curation_key` identity;
- task faceting is exactly once per exact `task_id`; its model-input digest is provenance, not the identity;
- model name and effective parser are provenance only. Changing either does not authorize another paid pass
  over unchanged input. A semantic prompt/envelope change must bump `input_schema`;
- `unavailable` does not consume a semantic key, while `empty`, `proposed`, `error`, an ambiguous paid attempt
  and `already-governed` are terminal for that key. The durable begun claim is written before provider I/O;
- a legacy v1 exact-run receipt or begun claim suppresses replay for that exact run only. Because its model
  input cannot be reconstructed, it is never promoted into a portfolio-wide v2 semantic receipt;
- on-demand CLI/owner-HTTP steward requests use an explicit `action_id`; new invocation rows bind it to a
  canonical request digest (never the raw request) before client construction. Reusing an id with another CLI goal, model,
  proposal budget or surface is rejected before paid work or replay. Legacy rows without this additive v1
  field remain replay-only because their original request cannot be reconstructed safely;
- these on-demand requests are a separate manual invocation path and remain proposal-only.

The three curation files are mixed-version invocation ledgers, not uniform lists of semantic receipts
(concept and claim additionally contain the on-demand HTTP rows):

| Row family | Identity and interpretation |
|---|---|
| legacy finalize v1 | exact `run_id`/`task_id` compatibility evidence with no reconstructable model-input digest; it suppresses only that run |
| on-demand CLI/HTTP v1 | `steward-invocation-begun` uses `invocation_id` and its terminal row uses the requested `action_id`; new rows repeat one private `request_digest` across both halves, while legacy rows remain replay-only without it. This is manual request idempotency, not finalize semantic identity |
| finalize diagnostic v2 | a source-keyed `*:diagnostic:v2:*` row records a failure before an exact model-input digest/key can be established; `input_digest` is empty, so this audit row is not a semantic portfolio receipt |
| finalize semantic v2 | concept/claim `curation_key` is the exact input digest; facets use the exact-task key and retain the input digest as provenance |

Two modules write those families, and they are deliberately separate transactions:
`engine/curation_protocol.py` is the unattended finalize one (semantic key, claim held in a
`.curation_invocations/` side file, one lock per key; a lost terminal is CLOSED by the next attempt
with `prior_attempt_incomplete_not_replayed`), and `engine/steward_invocation.py` is the on-demand
CLI/HTTP one (operator `action_id`, claim held as a `steward-invocation-begun` row in the ledger
itself, one lock per ledger; a lost terminal stays OPEN and the operator must review it and choose a
new id). They share exactly one thing by construction — the steward-kind to ledger-file table in
`engine/governance_health.py::curation_ledger_file`.

Finalize v2 rows carry `curation_key`, exact `source_key`, `run_id`, `task_id`, `finish_seq`,
`input_digest`, `input_schema`, redacted `model`, effective `parser`, `outcome` and a bounded proposal payload.
The source tuple is trigger provenance, not a fallback paid-work identity. Readers must branch on `v`,
`action` and key shape; in particular, they must not treat v1 rows or diagnostic v2 rows as portfolio-wide
semantic receipts.

The Research Atlas preview reads bounded concept/claim projections plus recent tails of the two curation
ledgers. It displays proposal counts and a small outcome allowlist; unrecognized/legacy outcomes collapse to
generic proposal copy.
It neither fetches the task-facets ledger nor exposes the semantic key, input digest/schema, source key, model
or parser, so the UI is not a billing audit surface. Each read does expose one opaque, replacement-sensitive
`portfolio_id` derived from the resolved configured directory identity. The browser refuses to mix identities
across its four independent slices. Typed governance bodies and paid steward queries must echo it as
`expected_portfolio_id`; a live `memory_dir`, symlink-target or directory replacement fails with 409 before
any write/provider setup. A read may expose an empty identity for a configured directory that does not yet
exist, but mutation against that provisional identity fails with `409 portfolio_not_initialized` before
ledger/provider setup; initialize the directory, refresh all slices, and form a new action against the
replacement identity. This storage fence is not a frozen corpus watermark or atomic evidence snapshot.

The run-end dependency order is: case/research claims/concept capsule → reflection → concept steward →
claim steward → task facets → final `llm_cost` → completion. Thus the claim steward sees the current
run-end reflection, and all steward inference is included in the final cost delta. The same frozen snapshot
that produced the digest is passed to the proposal call, preventing a memory reread from changing paid input
after the durable claim.

## Methodologies (how memory moves)

<!-- CODEX AGENT: "open hypotheses" now means every open Card work item, even when several native Cards
share one seed belief. The live prompt renders immutable `seed_statement`, not the operator-edited
`statement`; this table should not claim a single current belief projection until identity and edit
semantics are defined consistently. The belief KEY is no longer in question and this note used to say
otherwise: `grouped_beliefs()` keys on the full `hypothesis_statement_digest`, never the collision-prone
short `hypothesis_id`, and that digest is now PUBLISHED as the derived `Card.belief_id`
(`events/card_ledger.py::_apply_card_belief_lineage`) so the projection, `RunState.open_research_beliefs()`
and any future consumer read one spelling. `grouped_beliefs()` itself still has no production consumer.
Beside it, `Card.retry_of` names the card a `debug` card is a REPAIR of — the same belief AND the same
executable question, which is what made a debug retry render as a second identical hypothesis. -->

| Methodology | What it does | Touches |
|---|---|---|
| **Reflection / distillation** (run-end) | Distils the run into cross-run memory: the winner → a case; an explanatory hypothesis about *why it may have won* → a meta-note; an **LLM pass** consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one generalizable lesson per theme (no verbatim-hypothesis or templated-failure dump); a supported technique + its code → a skill. **A winner is not required**: the case, the meta-note and the skill all need one, but the lesson pass does not — a run in which every experiment crashed reflects over its *failures* (the prompt then asks what BLOCKED the work and points the model at `read_experiment`/`read_logs`, since `error_reason` is a one-word bucket). Only a run with no evaluated node, no failed node and no resolved card skips the call. The prose is model-authored interpretation, not causal identification. | cases, meta-notes, lessons, skills |
| **Task fingerprint + similarity** (M2) | A deterministic task descriptor (kind, direction, metric, goal keywords, param names); Jaccard overlap gates/ranks cross-run transfer to *similar* (not just identical) tasks. | lessons, skills |
| **Passive prompt-injection** (run-start + per-proposal) | Fingerprint-matched lessons + exact-task meta-notes + the always-on digests + open hypotheses are written into the proposal prompt; contradicted verdicts are quarantined (newest wins). | lessons, meta-notes, hypotheses, digests |
| **Role-split lesson routing** | Cross-run lessons are **tagged by role** at distillation and routed to only that role's context: the **Researcher** proposal prompt gets R&D / "what technique to try" lessons (the LLM reflection consolidation + improve-pair param credit); the **Developer** gets only its own "what code change fixed a crash" lessons (comparative *debug*-pair credit), folded into the idea it implements — most useful on repair. Untagged lessons are **shared** (both roles see them): legacy rows, an unattributed comparative line, and the lessons of a run that produced **no measured result** — those are findings about what blocked the work (library/API/hardware constraints), which is the Developer's category as much as the Researcher's. | lessons |
| **Active agentic retrieval** | Supported tool-using roles pull memory on demand (see below): Researcher, Strategist, deep research, Genesis, the in-house repo Developer and owner Assistant. Exact availability remains role- and feature-gated. | cross-run claims/concepts, siblings, own run, knowledge |
| **Harmonic indexing** (Memora) | Indexes by a short *abstraction* + cue *anchors*; consolidates near-duplicates at build time and expands retrieval through anchor links at query time. LLM-optional (degrades to lexical). | knowledge, cases, lessons |
| **Consolidation / hygiene** (D2) | Merges duplicate lessons into an `evidence_count`, retires contradicted verdicts, and bounds the store size. Dedup identity is `(statement, task, role)` — a Researcher and a Developer lesson with the same statement never collapse (merging would drop one role's copy and break the routing). On top of exact-normalized dedup, a **hybrid-retrieval (grep+BM25+vector, RRF) → agentic paraphrase-merge** pass (per `(task, role)`) lets the Researcher fold re-worded duplicates. | lessons |
| **Prompt-slot rationing** (read path) | The injected prior has a fixed budget — **5 lessons + 3 meta-notes**. Slots are rationed by a *presentation* key (`lesson_hygiene.prompt_slot_key`: the normalized statement with every **number** collapsed), so N rows of one f-string template ("changing x A→B regressed the metric by D") spend **one** slot and the rest go to genuinely different findings. Rows are ranked first (similarity → confidence × corroboration → recency), so the slot keeps the family's best row and renders it verbatim, digits intact. This is deliberately **lossier than the write-path `(statement, task, role)` identity** and never substitutes for it: consolidation must keep two measurements apart, while a prompt only needs the sentence once. Nothing is dropped from the store. | lessons, meta-notes |
| **Reconciliation on re-eval** (`lessons_reconciled`) | When a `node_reset` re-eval **flips a node's outcome** (a false-failure re-scored to evaluated, a demoted champion), this run's distilled lessons *grounded in that node* go stale. Each lesson stamps its evidence nodes' outcome **signature** at write time; a cheap `{node→sig}` hash gate detects the drift, then the stale lessons are **retired and re-derived** from the corrected state (same conclusion → identical lesson reappears = no-op; different → the stale row is replaced). Comparative lessons upsert per-pair (un-spend → re-derive → re-spend); reflect lessons re-derive the whole-run batch. Best-effort, LLM-only (never writes a template), replay-safe (idempotent — an empty re-derivation never nukes memory). | lessons |
| **Verification** (D8) | The evidence ledger — each research claim carries its citing `node_ids`/URLs so a verifier can check it. Verdicts do not re-rank the current run; at finalization, however, only an aligned `supported` verdict may back positive D8 cross-run claim evidence. | research memo, cross-run claims |

## Agentic retrieval — role-gated pull surfaces

The tool-using Researcher has the broad in-run surface below; other supported reasoning roles receive
the providers appropriate to their task. Prompt-injected material is listed separately because it is not
necessarily exposed as an on-demand tool.

| Memory | Tool(s) |
|---|---|
| Knowledge base + cases | `kb_search`, `list_notes`, `read_note` |
| Lessons | `search_lessons` — returns each claim's verdict + “N agreeing recorded observations; not independent verification”. Scoped to what the live run may see: same objective `direction` (rows without one are invisible), exact task **or** a strict goal-fingerprint overlap, and never the run's own rows. Same predicate as the `cross_run_*` tools; an unbound CLI/human reader stays portfolio-wide |
| Meta-notes | `recall_notes` — model-distilled explanatory hypotheses for this/similar task, not causal proof |
| Skills | `list_skills`, `use_skill` |
| Own experiments | `list_experiments`, `read_experiment`, `read_code` |
| Sibling runs (same task) | `list_sibling_runs`, `read_sibling_experiment`, `read_sibling_code`, `find_analogous_across_runs` |
| Part IV/V portfolio claims + concepts | `cross_run_prior_attempts`, `cross_run_claims`, `cross_run_atlas`, `cross_run_search`, `cross_run_concept_map`, `similar_runs`, `find_concept_slugs`, `concept_card` |
| Cards (open beliefs) | injected each proposal (open ones, with instruction to reuse exact wording for evidence linking) |

## The concept shelf — memory indexed by the concept tree

The Memory panel can be filtered and grouped by the same concept tree the run workspace shows, so
"what has this lab learned about `loss/contrastive`" is a question you can ask of lessons, cases and
meta-notes. `GET /api/memory` ships that join beside the rows, under `concept_index`
(`looplab/engine/concept_shelf.py::build_shelf` — a pure read projection; it never writes back).

Two things it publishes that matter when you read the result:

**Where a row's concepts came from.** Each attributed row carries `concept_source`:

| Source | Meaning |
|---|---|
| `record` | the row itself was tagged, at distillation time, from the experiments it describes |
| `run` | inherited through the row's `run_id` from its whole run's folded concept membership |

These are different claims. `record` says *this lesson is about that concept*; `run` only says *the
run that produced this lesson touched it*. The UI labels them separately for that reason.

**How much of the tier is covered at all.** `concept_index.coverage` states `total`, `tagged`,
`untagged` and the per-source split for each tier, and the panel renders it next to any concept
filter. On a real portfolio most rows predate the durable `concepts` field and most runs were never
tagged, so filtering would otherwise return an empty list that reads as "no such knowledge" when the
truth is "this knowledge was never tagged". An unstated count renders as `—`, never as `0`.

The shelf **does not invent concepts** — no keyword matching, no embeddings, no `task_id` guess. An
untagged row stays untagged and is counted as such. Deriving a concept from a lesson's text is a
tagger's job (`search/concept_tagging.py::tag_text_llm`), it costs a provider call, and its output
belongs in the durable field via the write path.

Run-level inheritance folds only the runs the loaded rows actually cite, so opening the panel does
not fold the whole workspace. When the run list cannot be read at all the shelf still ships with
whatever durable tags the rows carry, and `concept_index.runs_indexed: 0` says why the rest is
untagged.

## Configuration

- `LOOPLAB_MEMORY_DIR` — cross-run memory home (default `~/.looplab/memory`; `""` disables).
- `LOOPLAB_KNOWLEDGE_DIR` — knowledge base home (default `~/.looplab/knowledge`; `""` disables).
- `LOOPLAB_MEMORA` — harmonic indexing (abstraction+anchors) over the stores; **on by default**, set `=0`/`false` to restore the raw-text index.
- `LOOPLAB_RESEARCHER_TOOLS` — master switch for the tool-using Researcher (agentic retrieval); off → a plain researcher that only sees the injected memory.

The assistant can grow the knowledge base directly: share experiment results/lessons and ask it to
remember them, and it distils + saves a note via its `remember` tool. The tool is unavailable in
Plan mode and remains subject to the active mode's write permissions in mutating modes. See
[LLM & coding agents](llm-and-agents.md).
