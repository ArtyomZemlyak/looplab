# Memory & knowledge

LoopLab remembers. Across a run and across runs it accumulates several **distinct kinds of memory**,
each with its own purpose, and it both **injects** the relevant ones into the proposal prompt *and*
lets the agent **actively pull** any of them on demand. This page is the structured reference: the
types, what each is *for*, how it's written and read, and the methodologies that move data through it.

Both stores are **on by default** (a user asked for it): `~/.looplab/memory` (cross-run memory) and
`~/.looplab/knowledge` (the knowledge base). Relocate with `LOOPLAB_MEMORY_DIR` /
`LOOPLAB_KNOWLEDGE_DIR`, or set either to `""` to disable. See [Configuration](configuration.md).

## The types (what each is *for*)

Each type is deliberately different — they are **not** interchangeable:

| Type | Essence — what it's for | Stores | Scope | Written | Read |
|---|---|---|---|---|---|
| **Cases** (`cases.jsonl`) | *The list of the best* — the winning config per task, verbatim, retain-on-improvement. | `{task_id, goal, direction, params, metric, rationale}` | cross-run | run-end | `kb_search`, exact warm-start |
| **Meta-notes** (`meta_notes.jsonl`) | *Why it may have won* — a short, LLM-distilled explanatory hypothesis over the observed run (not the raw config — that's the case, and not causal proof). | `{task_id, note}` (model-authored explanatory prose) | cross-run (per task) | run-end (LLM; falls back to a stats line) | exact warm-start, `recall_notes` |
| **Lessons** (`lessons.jsonl`) | *Generalizable good **and** bad findings* — higher-level claims ("larger batch tends to help") with a verdict and a count of agreeing recorded observations, not independent verification. **Split by `role`** (see below). | `{statement, outcome: supported/tested/abandoned/failed/refuted/noted (action guidance; noted is neutral), claim_stance: support/oppose/neutral (relation of evidence to the literal statement on new rows), delta, confidence, evidence, evidence_sig (each evidence node's outcome signature at write time — the reconciliation provenance), evidence_count, fingerprint, role: researcher/developer (absent = shared)}` | cross-run (task-fingerprint matched) | run-end — **LLM-authored only**: the reflection consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one lesson per theme, plus M6 comparative code-fix pairs (offline/toy path: a deterministic winner record); also **re-derived when a re-eval flips a cited node** | prompt injection (role-routed, fingerprint-matched), `search_lessons` |
| **Skills** (`skills/*.md`) | *Best practices **with the script*** — a reusable technique + the code that implemented it; promoted when it re-confirms on a different task. | markdown: `name, description, status (candidate/promoted), fingerprints`, evidence, an **`### Implementation` code block** | cross-run (generalizes across tasks) | run-end (supported hypothesis, Δ>0) | `list_skills`, `use_skill` |
| **Knowledge base** (`knowledge/*.md`) | *Anything worth keeping* — free-form notes, hand- or agent-authored (the assistant's `remember` tool). | markdown notes | cross-run | assistant `remember`, by hand | `kb_search`, `list_notes`, `read_note` |
| **Hypotheses** (ledger, in-run) | *What's worth testing* — accepted "to-test" beliefs with a live **status** (open → testing → supported/tested/abandoned) and accumulating evidence. Deduped by exact hash **plus** an agentic paraphrase-merge (`hypothesis_merged` — the engine decides, the fold applies it deterministically, cadence open≥4 & grew≥2); the open board is prioritized by **foresight**. | `{statement, status, evidence, best_delta, source}` per node | one run (derived from the event log) | Researcher (`idea.hypothesis`) + `hypothesis_added` | injected into the proposal prompt |
| **Research memo** (deep-research) | *Breadth of scope* — a hard-thinking pass over all results plus enabled sources; its `recommended_directions` can **become hypotheses**. | `{summary, reasoning, findings, claims:[{statement, node_ids, urls}], sources:[{title,url,snippet}], recommended_directions, proposed_ideas, at_node, trigger, verification}` (`reasoning` is debug-only; `verification` is the persisted D8 verdict payload when available) | one run | on cadence / manual / strategist | folded into `RunState.research`, verified by the D8 verifier |
| **Exploits** (`exploits.jsonl`) | *Defensive memory* — patterns of cheating/leakage the trust layer scans for (co-evolved hacker-fixer). | `{name, pattern, kind}` | cross-run | `looplab harden` | reward-hack scan at eval |

In-run **working memory** (rebuilt from the event log each turn, never persisted separately): the
**hypotheses ledger**, the **diversity archive** (MAP-Elites elites/niches), and the **digests** —
`experiments_digest` (winners + failures + sweep landscape), `sibling_digest` (what siblings already
tried), `lineage_lessons` (subtree outcomes ranked by |Δ|), `ancestral_repair_chain` (prior repairs).
The append-only `events.jsonl` is authoritative for the replayable `RunState`. Task/config snapshots,
diagnostic spans, chat, command records and cross-run stores retain separate documented sidecar contracts;
`replay.fold` does not manufacture them.

## Current cross-run boundary and the Research Atlas target

The shipped memory above is useful, but it is not yet a complete scientific index over a large portfolio.
LoopLab now also has an **opt-in experimental Part-IV slice**: rebuildable run passports/facts, per-run
concept capsules with alias/split overlays, v2 persisted D8 claims, task-facet overlays, bounded hybrid
cross-run retrieval, and backend Atlas/claims projections. Bound pull tools apply role and compatible direction;
lessons/capsules accept exact task or a strict related-goal fingerprint, while v2 D8 (which stores no goal
fingerprint) is exact-task-only. Task facets are metadata reserved for future post-scope ranking and currently
neither grant visibility nor change ordering. External coding-agent Developer backends receive no D8 provider,
while the standalone CLI remains portfolio-wide. Proactive Researcher/Strategist influence persists lean
source/render digest receipts. Typed owner governance writes now have revision CAS, action-id idempotency and
explicit clear actions, while stewards remain proposal-only. These projections are real, but they do not yet
provide an immutable comparison/access scope, complete corpus-health/snapshot receipts, evidence/taxonomy
release identity, assignment backfill or independent evidence-family accounting. Typed owner HTTP concept
actions now validate live canonical merge/purge sources and merge targets; split may introduce provisional
children, but this is not a versioned taxonomy/entity release. Typed
claim decisions do fence a current claim and its observed evidence digest. An owner-only `#/atlas` **Experimental portfolio diagnostic** now renders the
bounded read models, but it is not the complete snapshot-consistent Research Atlas. The home Runs Map and a
run's theme grouping are different surfaces (see [Web UI](ui.md#which-graph-am-i-looking-at)).

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
- on-demand owner-HTTP steward requests use an explicit `action_id`; they are a separate manual invocation
  path and remain proposal-only.

The three curation files are mixed-version invocation ledgers, not uniform lists of semantic receipts
(concept and claim additionally contain the on-demand HTTP rows):

| Row family | Identity and interpretation |
|---|---|
| legacy finalize v1 | exact `run_id`/`task_id` compatibility evidence with no reconstructable model-input digest; it suppresses only that run |
| on-demand HTTP v1 | `steward-invocation-begun` uses `invocation_id` and its terminal row uses the requested `action_id`; this is manual request idempotency, not finalize semantic identity |
| finalize diagnostic v2 | a source-keyed `*:diagnostic:v2:*` row records a failure before an exact model-input digest/key can be established; `input_digest` is empty, so this audit row is not a semantic portfolio receipt |
| finalize semantic v2 | concept/claim `curation_key` is the exact input digest; facets use the exact-task key and retain the input digest as provenance |

Finalize v2 rows carry `curation_key`, exact `source_key`, `run_id`, `task_id`, `finish_seq`,
`input_digest`, `input_schema`, redacted `model`, effective `parser`, `outcome` and a bounded proposal payload.
The source tuple is trigger provenance, not a fallback paid-work identity. Readers must branch on `v`,
`action` and key shape; in particular, they must not treat v1 rows or diagnostic v2 rows as portfolio-wide
semantic receipts.

The Research Atlas preview currently reads only bounded tails of the concept and claim ledgers. It displays
proposal counts and a small outcome allowlist; unrecognized/legacy outcomes collapse to generic proposal copy.
It neither fetches the task-facets ledger nor exposes the semantic key, input digest/schema, source key, model
or parser, so the UI is not an identity or billing audit surface.

The run-end dependency order is: case/research claims/concept capsule → reflection → concept steward →
claim steward → task facets → final `llm_cost` → completion. Thus the claim steward sees the current
run-end reflection, and all steward inference is included in the final cost delta. The same frozen snapshot
that produced the digest is passed to the proposal call, preventing a memory reread from changing paid input
after the durable claim.

## Methodologies (how memory moves)

| Methodology | What it does | Touches |
|---|---|---|
| **Reflection / distillation** (run-end) | Distils the run into cross-run memory: the winner → a case; an explanatory hypothesis about *why it may have won* → a meta-note; an **LLM pass** consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one generalizable lesson per theme (no verbatim-hypothesis or templated-failure dump); a supported technique + its code → a skill. The prose is model-authored interpretation, not causal identification. | cases, meta-notes, lessons, skills |
| **Task fingerprint + similarity** (M2) | A deterministic task descriptor (kind, direction, metric, goal keywords, param names); Jaccard overlap gates/ranks cross-run transfer to *similar* (not just identical) tasks. | lessons, skills |
| **Passive prompt-injection** (run-start + per-proposal) | Fingerprint-matched lessons + exact-task meta-notes + the always-on digests + open hypotheses are written into the proposal prompt; contradicted verdicts are quarantined (newest wins). | lessons, meta-notes, hypotheses, digests |
| **Role-split lesson routing** | Cross-run lessons are **tagged by role** at distillation and routed to only that role's context: the **Researcher** proposal prompt gets R&D / "what technique to try" lessons (the LLM reflection consolidation + improve-pair param credit); the **Developer** gets only its own "what code change fixed a crash" lessons (comparative *debug*-pair credit), folded into the idea it implements — most useful on repair. Untagged (legacy) lessons are shared. | lessons |
| **Active agentic retrieval** | The Researcher *calls tools* to pull memory when it wants (see below). | all cross-run types + siblings + own run |
| **Harmonic indexing** (Memora) | Indexes by a short *abstraction* + cue *anchors*; consolidates near-duplicates at build time and expands retrieval through anchor links at query time. LLM-optional (degrades to lexical). | knowledge, cases, lessons |
| **Consolidation / hygiene** (D2) | Merges duplicate lessons into an `evidence_count`, retires contradicted verdicts, and bounds the store size. Dedup identity is `(statement, task, role)` — a Researcher and a Developer lesson with the same statement never collapse (merging would drop one role's copy and break the routing). On top of exact-normalized dedup, a **hybrid-retrieval (grep+BM25+vector, RRF) → agentic paraphrase-merge** pass (per `(task, role)`) lets the Researcher fold re-worded duplicates. | lessons |
| **Reconciliation on re-eval** (`lessons_reconciled`) | When a `node_reset` re-eval **flips a node's outcome** (a false-failure re-scored to evaluated, a demoted champion), this run's distilled lessons *grounded in that node* go stale. Each lesson stamps its evidence nodes' outcome **signature** at write time; a cheap `{node→sig}` hash gate detects the drift, then the stale lessons are **retired and re-derived** from the corrected state (same conclusion → identical lesson reappears = no-op; different → the stale row is replaced). Comparative lessons upsert per-pair (un-spend → re-derive → re-spend); reflect lessons re-derive the whole-run batch. Best-effort, LLM-only (never writes a template), replay-safe (idempotent — an empty re-derivation never nukes memory). | lessons |
| **Verification** (D8) | The evidence ledger — each research claim carries its citing `node_ids`/URLs so a verifier can check it (audit-only). | research memo |

## Agentic retrieval — the agent can pull *anything*

The tool-using Researcher can actively reach **every** memory type, so it's never limited to what was
auto-injected:

| Memory | Tool(s) |
|---|---|
| Knowledge base + cases | `kb_search`, `list_notes`, `read_note` |
| Lessons | `search_lessons` — returns each claim's verdict + “N agreeing recorded observations; not independent verification” |
| Meta-notes | `recall_notes` — model-distilled explanatory hypotheses for this/similar task, not causal proof |
| Skills | `list_skills`, `use_skill` |
| Own experiments | `list_experiments`, `read_experiment`, `read_code` |
| Sibling runs (same task) | `list_sibling_runs`, `read_sibling_experiment`, `read_sibling_code`, `find_analogous_across_runs` |
| Hypotheses | injected each proposal (open ones, with instruction to reuse exact wording for evidence linking) |

## Configuration

- `LOOPLAB_MEMORY_DIR` — cross-run memory home (default `~/.looplab/memory`; `""` disables).
- `LOOPLAB_KNOWLEDGE_DIR` — knowledge base home (default `~/.looplab/knowledge`; `""` disables).
- `LOOPLAB_MEMORA` — harmonic indexing (abstraction+anchors) over the stores; **on by default**, set `=0`/`false` to restore the raw-text index.
- `LOOPLAB_RESEARCHER_TOOLS` — master switch for the tool-using Researcher (agentic retrieval); off → a plain researcher that only sees the injected memory.

The assistant can grow the knowledge base directly: share experiment results/lessons and ask it to
remember them, and it distils + saves a note via its `remember` tool (works in any mode). See
[LLM & coding agents](llm-and-agents.md).
