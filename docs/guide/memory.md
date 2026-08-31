# Memory & knowledge

LoopLab remembers. Across a run and across runs it accumulates several **distinct kinds of memory**,
each with its own purpose. It **injects** selected priors into prompts and exposes a role-gated subset
through explicit tools; cards, curation ledgers and several UI projections are not generic pullable
memory. This page is the structured reference: the
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
| **Claims & Curation** | Lab → Claims & Curation | Claims (all of them, and the mixed-evidence subset) rolled up over *every* run in the memory dir, plus what the paid stewards proposed and what came of it | Derived at read time from what the runs wrote, plus your governance decisions | no (governance is CLI/HTTP) |
| **Concepts** | Runs → Concepts | The cross-run concept map: an `is_a` forest, co-occurrence, and the lessons/cases/notes carrying each concept | Derived from the run rows' own concept rollup | no |

Three consequences worth stating plainly, because each of them has been read as a bug:

- **Skills and prompts are not missing from Memory — they are on the Authoring panel**, because a
  human writes them. They are just as durable as a lesson.
- **`knowledge` appears on both panels.** It is the one kind both you and the agents write, so
  Authoring shows it writable and Memory shows it read-only. Same directory, same files.
- **A claim on Claims & Curation will not appear in Memory under that name.** Claims are not a fourth store:
  `engine/claims.py` groups the *same* `lessons.jsonl` rows by normalized statement and joins them
  with the deep-research memo claims in `research_claims.jsonl`. That screen is a view, not a tier.

## The types (what each is *for*)

Each type is deliberately different — they are **not** interchangeable:

| Type | Essence — what it's for | Stores | Scope | Written | Read |
|---|---|---|---|---|---|
| **Cases** (`cases.jsonl`) | *The winning run's exact configuration* — the parameter dict that produced the best metric on this task, kept machine-readable so the next run can start from it instead of re-deriving it from prose. Exactly one active row per `(task_id, direction)`: a leaderboard, not a history. | `{task_id, goal, direction, params, metric, rationale}` | cross-run | run-end | **exact-task warm start** (one line in the Researcher prior, beside the meta-note) + `kb_search` (see [What are cases for?](#what-are-cases-for)) |
| **Meta-notes** (`meta_notes.jsonl`) | *Why it may have won* — a short, LLM-distilled explanatory hypothesis over the observed run (not the raw config — that's the case, and not causal proof). | `{task_id, note}` (model-authored explanatory prose) | cross-run (per task) | run-end (LLM; falls back to a stats line) | exact warm-start, `recall_notes` |
| **Lessons** (`lessons.jsonl`) | *Generalizable good **and** bad findings* — higher-level claims ("larger batch tends to help") with a verdict and a count of agreeing recorded observations, not independent verification. **Split by `role`** (see below). | `{statement, outcome: supported/tested/abandoned/failed/refuted/noted (action guidance; noted is neutral), claim_stance: support/oppose/neutral (relation of evidence to the literal statement on new rows), delta, confidence, evidence, evidence_sig (each evidence node's outcome signature at write time — the reconciliation provenance), evidence_count, fingerprint, role: researcher/developer (absent = shared)}` | cross-run (task-fingerprint matched) | run-end — **LLM-authored only**: the reflection consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one lesson per theme, plus M6 comparative code-fix pairs (offline/toy path: a deterministic winner record); also **re-derived when a re-eval flips a cited node** | prompt injection (role-routed, fingerprint-matched), `search_lessons` |
| **Skills** — hand-written (`skills_dir`) | *Best practices **with the script*** — a reusable technique + the code that implemented it, offered to the Researcher as a tool. | markdown: `name`, `description` frontmatter + the technique in the body | cross-run | **by you**; root Markdown is editable and nested `**/SKILL.md` packages are review-only in Lab → Authoring → skills | `list_skills`, `use_skill` |
| **Skills** — auto-distilled (`<memory_dir>/skills/auto-*.md`) | The same tool surface, filled by the run itself from a card that was supported with Δ>0 and passed the portability classifier. | as above, plus `status (candidate/promoted)`, `provenance`, `claim_sha256`, `classifier_version`, `source_statement_sha256`, `source_task`, `fingerprints`; the skill reader parses the trust fields | cross-run | run-end (supported card, Δ>0) — the WRAP-UP pass, not a clean finish: `looplab finalize` performs it on a stopped run and is idempotent, so a run that was stopped or killed still gets it, but only if someone asks | A one-run *candidate* stays on disk but is hidden from the production `list_skills` / `use_skill` surface. Agentic classification binds promotion evidence to a validated canonical technique key; the offline/legacy path uses the full normalized claim. The readable prefix is never identity. A later sufficiently different task fingerprint (Jaccard similarity `< 0.6` to stored evidence) promotes it. A subsequently constructed toolset lists/loads it with `UNTRUSTED_MEMORY_AUTO_SKILL` provenance |
| **Knowledge base** (`knowledge/*.md`) | *Anything worth keeping* — free-form notes, hand- or agent-authored (the assistant's `remember` tool). The one kind **both** you and the agents write. | markdown notes | cross-run | assistant `remember`, or Lab → Authoring → knowledge | `kb_search`, `list_notes`, `read_note` |
| **Prompts** (`<prompt_dir>/<key>.md`) | *What registered prompt consumers are told* — an override that REPLACES a matching built-in system prompt and is re-read when that call site renders it. Not learned and never written by a run: operator configuration that happens to live on disk. | one Markdown body per key in `core/prompts.py::PROMPT_KEYS` | global (a flat `Settings` field) | **by you**, in Lab → Authoring → prompts | registered `render(prompts, key, default)` call sites only; several assistant/report/monitor families still have separate prompt governance |
| **Cards** (work-item + belief board, in-run; one belief may have several cards) | *What's worth testing* — accepted work items with a live **verdict** (open → testing → supported/tested/abandoned) and accumulating evidence. `belief_id` is the full normalized `seed_statement` digest; the Researcher and foresight collapse open, untested cards by that identity so duplicate work items do not become duplicate beliefs. Agentic paraphrase merges (`hypothesis_merged`) remain a separate, durable relation. | `{id, belief_id, seed_statement, statement, verdict, evidence, best_delta, source, retry_of}` per card | one run (derived from the event log) | Researcher (`idea.hypothesis`) + `hypothesis_added` | one representative per open, untested belief is injected into the proposal prompt |
| **Research memo** (deep-research) | *Breadth of scope* — a hard-thinking pass over a bounded, lifecycle-aware stratified coverage sample plus enabled sources. The prompt reserves the leader/champion, then covers early seeds, eligible top metrics, representative genuine failures, recent active work and deterministic middle experiments. Tombstoned/aborted rows and durable pre-dispatch discards are excluded from experimental evidence and separately counted; constraint/trust-ineligible rows selected by a non-top bucket are labelled. The prompt reports exactly how many active experimental rows were omitted. The shared provider assembly exposes Run/Data, Sibling/AllRuns, CrossRun, Knowledge/Memory/Skills and gated Literature/Web tools; its `recommended_directions` can **become belief cards** — bounded at the append site: a direction that restates an open belief is not registered, and the open belief board is capped at five distinct rows (the window the prompts can show). The memo prompt itself now renders that board, both halves, in the proposal prompt's vocabulary. | `{summary, reasoning, findings, claims:[{statement, node_ids, urls}], sources:[{title,url,snippet}], recommended_directions, proposed_ideas, at_node, trigger, verification}` (`reasoning` is debug-only; `verification` is the persisted D8 verdict payload when available) | one run | on cadence / manual / strategist | folded into `RunState.research`, verified by the D8 verifier |
| **Exploits** (`exploits.jsonl`) | *Defensive memory* — patterns of cheating/leakage the trust layer scans for (co-evolved hacker-fixer). | `{name, pattern, kind}` | cross-run | `looplab harden` (CLI only — no UI) | reward-hack scan at eval. **The only cross-run store that can change a node's fate**, via `trust_gate` |
| **Concept capsules** (`concept_capsules.jsonl`) | *What this run tried, as concept slugs* — one capsule per run, with a per-concept outcome sign relative to that run's own field. The unit the Atlas aggregates. | v2 record: `{v, run_id, task_id, fingerprint, direction, concepts, concept_outcomes, concept_signs, best_metric}` + source-completeness receipts | cross-run | run-end, gated on `cross_run_concepts` | Atlas, the Researcher context pack, the Strategist note, seven `cross_run_*` tools, and `grade_novelty(prior_concepts=…)` — **surfaced, never a rejection** |
| **Research claims** (`research_claims.jsonl`) | *What a deep-research memo asserted, with its citations* — the counter-evidence half of a claim: the only source that can make a claim **contested**. | v3 record: `{v, record_kind, run_id, task_id, direction, statement, metric, node_ids, urls, verification, source_receipt}` | cross-run (exact task) | run-end, from the D8 memo ledger | claim/atlas projections → context pack, Strategist note, `cross_run_claims`/`atlas`/`search`, `/api/cross-run/claims` |
| **Governance policy** (`claim_decisions.jsonl`, `concept_aliases.jsonl`, `concept_splits.jsonl`) | *Your* corrections to the portfolio — ratify/reject/pin a claim; merge, purge or split a concept slug. Applied at READ time, so nothing is destroyed. | one action row each, with `action_id` + revision CAS | cross-run | **you** — `looplab claim-decide` / `concept-merge` / `concept-split`, or the `/api/cross-run/*` routes — plus, for concept MERGES only and only when `concept_tidy` is on, the ratification stage applying what the steward already proposed (`by=concept-ratifier/v1`, undone by the same `concept-alias-clear` you would use on your own row) | overlays every claim and concept projection an agent sees |
| **Steward proposals** (`*_curation_log.jsonl`) | *What a paid steward suggested* — a review queue plus the idempotency ledger that stops the same paid call being charged twice. | `{v:2, curation_key, input_digest, model, outcome, proposals, …}` | cross-run | run-end concept/claim stewards (`cross_run_curation`), optional run-end facet steward (`task_facets_finalize`), or the CLI/HTTP steward commands | **nothing reads these into a prompt or a decision.** They are an audit trail and a human queue: a proposal changes nothing until you issue the governance write above |
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

**In one line: a case is the winning run's exact configuration — the parameter dict that produced the
best metric on this task, kept machine-readable so the next run can start from it instead of
re-deriving it from prose.** That is the whole distinction from its neighbours: a *lesson* is a
generalizable claim, a *meta-note* is the causal story of why a run won, and a *case* is the recipe.

**Until 2026-08-19 that was true of the FILE and of nothing else, and this section said so.** A case
was written once per run and injected into nothing: `JsonlCaseLibrary.search()` and `.all()` had no
production call sites at all, and the store's only reader was `KnowledgeTools`, which embeds a case
into the same `kb` index as the knowledge notes — so a case reached a model only if a tool-using role
called `kb_search` AND the row won a top-3 semantic ranking against every note. Two things were
measured on 2026-08-19 and both are why the kind was fixed rather than deleted:

* **The meta-note does not cover it.** On the shared store's 30 cases — 29 `toy_quadratic` and one
  real row — the toy note genuinely IS the case's twin (`best metric 4.483 via op 'improve' params
  {'x': 0.885, 'y': -0.9026}` carries both parameters inline), which is why folding cases into notes
  looks right from that corpus. On the one real row it is not: `rubertlite-dr-unified-v8`'s note is a
  causal narrative naming ONE hyperparameter (`R-Drop … at α=0.5 … lifted recall from 0.7384 to
  0.762`), while its case carries fifteen — `loss.temperature 0.05`, `loss.thr 0.1`,
  `train.negatives.mining_type 1`, `batch_size 8192`, `gradient_accumulation_steps 2`,
  `learning_rate 0.001`, `max_grad_norm 1.0`, `n_epochs 15`, `warmup_ratio 0.2`,
  `weight_decay 0.1` … — beside `metric 0.762048`. Deleting the kind deletes the only re-runnable
  record of the only real run in the store.
* **Even the one reader it had never delivered that payload.** A `kb_search` hit is head-clipped at
  600 chars and the record used to lead with the `goal`, so on that same real case `best params=`
  began at char **691** of a 1,610-char record and could not fit — and because the scope gate admits
  a case only on an exact task id or a strict goal-fingerprint overlap, the 600 chars that did arrive
  were the reader's own task prompt restated. The toy cases fit (two parameters), which is exactly
  why this survived the life of the store.

**What reads a case now.**

* **The exact-task warm start** — `engine/lessons_priors.py::_scan_prior_context`, the loader that
  already reads the meta-note out of the file next door, under the same `(task_id, direction)` key
  and the same fail-closed `LessonScope` (exact task + compatible polarity + not this run's own row),
  now reads the active case beside it and renders ONE line into the **Researcher** prior:
  `Best known configuration for this task (the winning run's own parameters, not a recommendation):
  metric … (run …) with params …`. Same role gate as the notes — the Developer never sees it, because
  a hyperparameter set is research context. It withholds nothing and decides nothing: it is prompt
  text, so no metric, champion, selectability decision or violation can move (docs/36).
* **`kb_search`**, as before, but the record now leads with its payload — `PAST CASE (task,
  objective) metric=…, run …: params=… why: … measured on this goal: …` — so the params survive the
  hit clip, and the clip now says how much it cut instead of ending mid-recipe in silence.
* `GET /api/memory` renders them on the Memory panel.
* `JsonlCaseLibrary._reload` reads them back so `add` can keep the better metric.

`JsonlCaseLibrary.search()` and `.all()` still have no production call sites, and that is now a
statement about those two METHODS rather than about the kind: the readers above go through the same
bounded-window scan (`core/memory_window.py`) every other cross-run reader uses. The
vector/Memora-capable `CaseLibrary` in the same module remains explicitly unwired
(`tests/test_case_store_wiring.py` holds that line).

### `GET /api/memory` and what it can honestly say about one experiment

`GET /api/memory`, the explicit lesson/note tools, passive proposal priors, and the cases half of
`kb_search` share one **bounded recent JSONL snapshot** rule (last 2 MiB / 1000 lines, 128 KiB per
row). Each snapshot carries a SHA-256 digest, source row/byte counts, truncation, skipped-row and
availability truth, so a human can identify the same source window that influenced an agent. The API
returns at most 200 projected rows and each tier ships a receipt — `limit`, `returned`, `skipped`
(unreadable rows), `filtered` (excluded by the run filter below), `superseded` (inactive case-source
contributions), `source_window_truncated`, `unavailable`. The page also reports whether the run concept
index was available; an index failure makes the projection partial rather than looking like a healthy
zero. `?run_id=` narrows all three tiers to rows naming that run; it is applied **inside the
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
* the **cross-run store** is the mutable half, and consolidation replaces a group with one current
  base row. Base-specific fields such as its fingerprint/concepts remain authoritative; the agentic
  pass may replace the statement with LLM-written text and superseded rows are physically removed.
  Bounded `evidence_refs` and traceable/untraceable counts preserve source lineage across that merge,
  but they are not a statement redirect: a merged-away sentence is still reported as no longer present
  as written;
* **cases** carry no node id at all, and **meta-notes** carry no node id (and no `run_id` at all when
  written off the finalize path);
* run-end **reflect** lessons ride on the diagnostic `reflection_note` event, which carries neither
  `lesson_id` nor evidence, so they are attributable at run level only;
* modern **knowledge notes** written by `remember` carry actor/surface/time provenance; legacy notes
  remain unattributed, and neither shape is joined to a run/node without explicit source refs.

That is the complete list for PROVENANCE. It is not a statement about reach: a case now rides the
exact-task warm start beside the meta-note (see [What are cases for?](#what-are-cases-for)), and it
still carries no node id — what it carries is the configuration, not the experiment that produced it.

The case remains keyed by `task_id`, so on the similar-task path it cannot transfer at all (lessons
and capsules carry the goal fingerprint that makes fuzzy transfer possible; a case does not), and
`repo_task` is one task id across several repos — which is why the rendered line names the run it
came from and the goal it was measured on, and calls itself the winning run's own parameters rather
than a recommendation.

**If the merge still tempts you** — folding cases into meta-notes — the cost is: (1) `cases.jsonl` is
unversioned by contract (`valid_case_record` quarantines any row carrying `v` or `record_kind`), so it
cannot be migrated in place, only rewritten under a new filename; (2) `KnowledgeTools` would lose its
only non-note corpus and `kb_search`'s behaviour would change for every existing memory dir; (3) the
params are the one artifact a human can paste into a re-run, and prose is not a substitute — measured
above on the one real row in the store, where the note names one hyperparameter of fifteen.

### Authoring vs Memory

The boundary is **direction of authorship**, and it is principled — but only one word on each panel
ever said so, and neither said it about the other. Authoring is the three directories a human fills
(`prompt_dir`, `skills_dir`, `knowledge_dir`); Memory is what a run appends when it finishes. Both
are durable, both are cross-run, both are read by the same agents.

Two things blur it, and both are real:

* **`knowledge` is on both panels** — writable in Authoring, read-only in Memory — because it is the
  one directory both parties write. That is not a bug, but nothing in the product said so.
* **Auto-distilled skills cross the human/run boundary.** `write_auto_skill` puts run-authored skills in
  `<memory_dir>/skills/`, and `SkillTools` reads them alongside the hand-written ones. So there is
  machine-written content on the reusable-skill side of the line — and it is visible in **neither** panel:
  the Memory endpoint only serves `cases`/`lessons`/`meta_notes`, and `GET /api/skills` resolves
  `settings.skills_dir`, which is a different directory (and `None` by default).

### Skills and prompts

Both are real durable kinds and both are already in the product — on the **Authoring** panel, because
a human writes them. Three concrete facts sit behind "why aren't they there":

1. **`skills_dir` and `prompt_dir` default to `None`.** Out of the box both Authoring tabs render
   `no skills dir configured` / `no prompts dir configured` — indistinguishable, to a new operator,
   from "this feature does not exist". `memory_dir` and `knowledge_dir` default to `~/.looplab/…`;
   these two do not.
2. **Configured recursive packages now have a bounded review surface.** `SkillTools` discovers
   `**/SKILL.md` and root `*.md`; Authoring mirrors that inventory, while preserving the authority
   boundary: root Markdown is writable through flat CAS/recovery identities, but nested packages use
   safe relative display names and are read-only. Symlinks/path escapes are skipped. A directory,
   entry or depth cap is disclosed independently from the known lower bound of omitted files, so a
   partial inventory is never treated as proof that a retained file was deleted.
3. **A claim only becomes a skill if it could transfer.** The promotion gate used to ask three
   things — is the card supported, did it move the metric, is the statement non-empty — and none of
   them is "does this generalize". Measured 2026-08-12 over the 27 auto-skills a real store had
   accumulated, **every one was instance-specific**: `perturb best node 8 (metric=5.4404437)`,
   `perturb node 9 (params={'x': 3.7898})`, `mean-merge of nodes 0,1` — the same operation five
   times under five node numbers, as five separate "skills".
   The replacement is a hybrid quality gate:

   * a bounded, NFKC-normalized deterministic prefilter rejects local node/trial references,
     measurements, parameter literals, local/vague pointers and content-free advice. It returns a
     stable reason code and avoids a paid call for impossible candidates;
   * when a reflection client exists, a structured rubric independently scores seven closed axes:
     procedural, actionable, non-obvious, evidence-grounded, transferable, single-technique and
     instance-detail-free. The model never emits the acceptance decision — code derives it from all
     seven fields and fails closed on a missing or malformed rubric;
   * an accepted rubric must produce a portable title and a compact canonical technique key. Code
     re-runs the prefilter, checks that the title still shares the evidenced subject, and binds most
     key vocabulary to that title before the key can become lifecycle identity. That lets two honest
     paraphrases confirm one technique without giving fuzzy text similarity promotion authority;
   * a client-less/offline run retains the strict deterministic path for compatibility. A configured
     classifier failure skips the skill safely because the underlying lesson has already been
     retained. Every considered positive card gets a versioned accept/reject receipt and reason in
     the run's `reflection_note` event; source text is represented in frontmatter by SHA-256 only.

   **There is a rung BEFORE both of those, and since 2026-08-19 it leaves a receipt too.** A card is
   only offered to the prefilter if it is `supported` **with a positive `best_delta`**, and that
   eligibility gate used to be a silent `continue` — so the receipt built to answer "which statements
   were refused and why" was blind to the rung that refuses most. Measured over this box's preserved
   runs, it is the ONLY thing that fired on the two finished Card-era runs whose `reflection_note`
   recorded `n_skills: 0`: one had no evaluated node at all, and all three `supported` cards of the
   other are **record setters** (a card is supported when one of its nodes sets the run's SOTA, and
   `best_delta` stays `None` when that node has no evaluated parent to have improved over). So a
   `n_skills: 0` was read for days as "the classifier is over-rejecting" when the classifier was
   never asked. A refused-here card now records `reason: no_measured_delta` or `no_positive_delta`
   under `classifier: "skill-eligibility/v1"`, for `supported` cards only — a `tested`/`open` card is
   not a candidate in any sense. The RULE is unchanged: a technique card claims "this improved the
   metric over its baseline", and a record set with no baseline is not that claim.

   This induction/verification split follows the direction of
   [MIND-Skill](https://arxiv.org/abs/2605.08670) (separate reusable procedure from instance leakage)
   and [Skill-DisCo](https://arxiv.org/abs/2606.26669) (normalize a reusable procedure before a
   separate verification stage). LoopLab's verification stage remains its existing independent
   confirmation on a sufficiently different task fingerprint; it does not claim held-out benchmark
   execution. Refusing a SKILL never refuses the LESSON — only the procedural tier is gated.
4. **Auto-distilled skills have no first-party review surface** (see above). Their read path does
   enforce the promotion lifecycle: `write_auto_skill` keeps a candidate on disk and accumulates
   task fingerprints under an interprocess lock; production `SkillTools` hides that candidate until
   a later fingerprint with Jaccard similarity `< 0.6` to stored evidence promotes the same file.
   Visibility changes when a new `SkillLibrary`/toolset is constructed; existing toolsets do not
   hot-reload. `SkillLibrary(...,
   include_auto_candidates=True)` is an explicit inspection/test seam, not a runtime setting. Every
   loaded auto-skill is labelled `UNTRUSTED_MEMORY_AUTO_SKILL`; hand-written skills retain their
   legacy body and visibility semantics.

### Kinds nothing reads back

Written durably, at cost, and consumed by no prompt and no decision:

* **Task facets** — an LLM classification per task. The module's own docstring says they "do not
  currently change retrieval order"; the only reader of the content is the writer's own
  once-per-task dedup. Fresh configurations therefore do not schedule this paid run-end call;
  `task_facets_finalize` explicitly opts in without removing the manual/on-demand APIs.
* **Steward curation logs** — by design: the stewards are proposal-only, and the log is the human
  review queue plus the paid-call idempotency ledger. Worth knowing it is not memory the agents read.
* **Auto-skill lifecycle metadata** — `claim_sha256` binds evidence to the complete normalized
  technique, and `fingerprints` drive the cross-task promotion decision; `source_task` is audit
  provenance only. None is returned to a reasoning role; `status` gates visibility and
  `provenance` labels the skill read path as described above.

## The evaluation contract: what a number from another run means

A shared `task_id` is an operational lookup key. It does **not** bind the metric's name, its unit, the
dataset, or the harness that produced it — so two runs listed side by side may have optimized the same
metric name against different corpora with different scorers. `ui/src/crossRunRank.js` has refused to
claim otherwise since it shipped; since 2026-08-16 the run-reading **tools** state the same boundary as
a fact on the row instead of leaving it to be inferred.

`looplab/engine/eval_contract.py` reads one run's own `task.snapshot.json` — the operator's declaration,
written by the engine at setup — and derives its **evaluation contract**: the metric reader (`kind` plus
the pattern/key it matches), the eval `command`, and the declared artifact/data paths. Two runs are
comparable when those are equal. Deliberately **not** part of the identity: `direction` and `task_id`
(the existing scope predicates already gate on both), the goal prose (it drifts between reruns of one
evaluation), and timeouts/seeds/footprints (they change what a run costs, never what its number means).

**The metric name is not the key, and this is the trap.** Measured on this box (2026-08-16, 46 run
directories with a `task.snapshot.json`): `stdout_regex` / `RECALL@100: ([0-9.]+)` is byte-identical
across three different contracts —

| contract | eval command | declared paths | runs |
|---|---|---|---|
| `repo_task` | `python -m vectorsearch.test` | `/home/jovyan/data/vectorizer-unified` | v2, v6, v7, v8 |
| `rubert_dr_0804` / `rubert_dr_0807` | `python looplab_eval.py --save_path models/rubertlite_run --gpus 1` | `…/vectorizer/dense-retrieval`, `…/datasets/dense-retrieval/rubertlite` | 0804, 0805, 0807 |
| (the human's own harness) | operator-declared | `…/vectorizer/dense-retrieval` | `rubertlite-dense-retrieval` |

Partitioning on the metric name would merge exactly the runs this exists to separate. `(task_id,
direction)` is wrong in **both** directions on the same corpus: it *under*-splits (`rubertlite-dense-
retrieval` folds to `repo_task` and is listed as a sibling of v8, with its `best=0.8077`, while running a
different harness) and it *over*-splits (`rubert_dr_0804` and `rubert_dr_0807` are the same contract
under two task ids).

**What it does.** `SiblingRunTools`, `AllRunsTools` and their shared `ForeignRunReader` plumbing append a
deterministic receipt beside another run's number: a short suffix on a listing row
(`· DIFFERENT EVAL CONTRACT (not this run's scale)`) and, on a per-experiment read, one sentence naming
which facet differs, placed **before** the metric line it qualifies.

**What it does not do**, and each of these is load-bearing:

* It **withholds nothing**. The number is still shown, the code is still readable, the params are still
  there. It annotates.
* It **fails open**. A contract that cannot be read is `None` — a third answer, never "different". Of the
  46 runs with an event log, 12 have no readable contract and are never flagged. Hiding a legitimate
  prior result would be worse than the defect.
* It **cannot reach the record**. Every consumer is a tool output string; nothing touches `RunState`,
  writes an event, or is read by `fold`. No metric, champion, selectability decision or violation can
  move on it (`docs/36`).
* It is **silent for the operator's assistant**. `MachineRunsTools` binds no self run, so portfolio reads
  by a human are unchanged byte for byte. The boundary is about what a *run* treats as its target.

**What it does not reach, stated rather than patched.** A foreign number quoted inside a *native* row's
prose is invisible to any row-level rule. `rubertlite-dr-unified-v7` is a genuine `repo_task` run, and it
published: *"…0.8173 at temp 0.01 and 0.8651 at temp 0.05, both below the 0.8776 symmetric-InfoNCE
baseline."* Two of those floats are v7's own and legitimately comparable; the third is `rubert-dr-0807`'s
and is not. One sentence, three numbers, two provenances — no deterministic rule separates them, which is
why nothing here tries to, and why a value-stripping filter over the cross-run **memory** stores is not
implementable. See `docs/BACKLOG.md` §0.6.

The cross-run memory rows carry no contract at all: measured over the live store, **0 of 132 rows** (23
lessons, 63 research claims, 4 capsules, 21 cases, 21 meta-notes) carry a metric name, a dataset path, an
eval command or a contract identity, while 132 of 132 carry `run_id` and `task_id`. A retrieval partition
keyed on a stored contract is therefore inert on the existing corpus and a fail-closed one would blank it
entirely — which is why this change is at the tool surface, where the run directory can be read, and not
at the store.

## Current cross-run boundary and the research-index target

The shipped memory above is useful, but it is not yet a complete scientific index over a large portfolio.
LoopLab also ships an **experimental Part-IV slice enabled by default in product `Settings`** (the
bare-library `EngineOptions` defaults remain off): rebuildable run passports/facts, per-run
concept capsules with alias/split overlays, v3 persisted D8 claims, task-facet overlays (manual or
explicitly scheduled with the default-off `task_facets_finalize`), bounded hybrid
cross-run retrieval, and backend Atlas/claims projections. Bound pull tools apply role and compatible direction;
capsule upsert identity and current-run exclusion key on `run_uid` — a persisted portfolio-wide
run-incarnation UID minted at `run_started` (`engine/orchestrator.py`), with the display `run_id` kept as
the human-facing name. Two independent run roots that reuse a local run id therefore no longer replace
each other's capsule. The caveat that remains is historical only: rows written before the UID existed
carry no `run_uid`, so they still fall back to matching on the display id alone.
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
claim decisions do fence a current claim and its observed evidence digest. An owner-only `#/claims`
**Experimental portfolio diagnostic** now renders the bounded read models. Its claim/evidence slices carry
coherent source identity, but the four independently fetched projections are not the complete canonical
cross-run research index. That screen shipped as the *Research Atlas* and was renamed **Claims & Curation**
by doc 29 F7, which also dropped its concepts section: the run list's **Concepts** view is strictly richer
(a full `is_a` forest, co-occurrence, a per-concept detail pane, and the lessons/cases/notes carrying each
concept), and the old name is what made an operator expect a concept map and then find a worse one. The
`/api/cross-run/*` routes did NOT move — `/api/cross-run/atlas` still serves the mixed-evidence claim
records the screen reads. The home Runs Lineage view and a
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
the claims endpoint/CLI, and the Claims & Curation screen disclose the lower bound. The producer-prefixed receipt remains
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

The broader Part-IV design specifies the production **cross-run research index** and its UI.
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

The Claims & Curation preview reads bounded claim projections plus recent tails of the two curation
ledgers (it still reads `/api/cross-run/atlas`, for the mixed-evidence records only). It displays proposal counts and a small outcome allowlist; unrecognized/legacy outcomes collapse to
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
claim steward → optional task facets (`task_facets_finalize`) → final `llm_cost` → completion. Thus the
claim steward sees the current run-end reflection, and every scheduled steward's inference is included in
the final cost delta. The same frozen snapshot
that produced the digest is passed to the proposal call, preventing a memory reread from changing paid input
after the durable claim.

## Methodologies (how memory moves)

| Methodology | What it does | Touches |
|---|---|---|
| **Reflection / distillation** (run-end) | Distils the run into cross-run memory: the winner → a case; an explanatory hypothesis about *why it may have won* → a meta-note; an **LLM pass** consolidates the run (worked/failed nodes + resolved hypotheses + failure themes) into one generalizable lesson per theme (no verbatim-hypothesis or templated-failure dump); a supported technique + its code → a skill. **A winner is not required**: the case, the meta-note and the skill all need one, but the lesson pass does not — a run in which every experiment crashed reflects over its *failures* (the prompt then asks what BLOCKED the work and points the model at `read_experiment`/`read_logs`, since `error_reason` is a one-word bucket). Only a run with no evaluated node, no failed node and no resolved card skips the call. The prose is model-authored interpretation, not causal identification. | cases, meta-notes, lessons, skills |
| **Task fingerprint + similarity** (M2) | A deterministic task descriptor (kind, direction, metric, goal keywords, param names); Jaccard overlap gates/ranks cross-run transfer to *similar* (not just identical) tasks. | lessons, skills |
| **Passive prompt-injection** (run-start + per-proposal) | Fingerprint-matched lessons + exact-task meta-notes + the always-on digests + up to five open, untested belief representatives are written into the proposal prompt. Beliefs are collapsed by full `Card.belief_id` and rendered with immutable `seed_statement`, not the operator-edited display statement; contradicted verdicts are quarantined (newest wins). | lessons, meta-notes, cards/beliefs, digests |
| **Role-split lesson routing** | Cross-run lessons are **tagged by role** at distillation and routed to only that role's context: the **Researcher** proposal prompt gets R&D / "what technique to try" lessons (the LLM reflection consolidation + improve-pair param credit); the **Developer** gets only its own "what code change fixed a crash" lessons (comparative *debug*-pair credit), folded into the idea it implements — most useful on repair. A debug pair is either a failed parent with a succeeding child or the **in-node repair** of a single node (the same node before and after its own fix), and the in-node one is sourced from `RunState.repair_ledger`: what the node was repaired FOR, which files each repair changed and what it said it was doing. It has to be — a node that failed, was repaired in place and then SCORED carries no `failed_stage` and no `error_reason` (only the failed terminal writes them, and every reset clears them) and keeps no superseded stage row either (stage rows are folded last-wins by name, so the retry that succeeded replaced the one that failed); measured 2026-08-30 over every event log on the box, that is 0 of 23 for both, against 19 of 23 with a ledger reason and 22 of 23 with ledger paths. When the ledger names no cause the lesson names none — it never invents a stage. A role that is NEITHER producer — the **Strategist**, which decides policy over both — sees **every** lesson, tagged or not: the split routes two kinds of finding to the two roles that can act on each, and a meta-decision role is not a third audience to filter for. Both readers of the store now spell that the same way (`tools/cross_run_tools.py::_role_lessons` always did; `tools/memory_tools.py` did not until 2026-08-30, so a Strategist read the store as a Researcher and silently lost every developer row — 4 of the 50 on this box). The two halves of that fix ship together on purpose: forwarding the real role WITHOUT the unknown-role escape is worse than the defect, because a `strategist` then matches no tagged row at all and keeps only the 10 untagged ones. Untagged lessons are **shared** (both roles see them): legacy rows, an unattributed comparative line, and the lessons of a run that produced **no measured result** — those are findings about what blocked the work (library/API/hardware constraints), which is the Developer's category as much as the Researcher's. | lessons |
| **Active agentic retrieval** | Supported tool-using roles pull memory on demand (see below): Researcher, Strategist, deep research, Genesis, the in-house repo Developer and owner Assistant. Exact availability remains role- and feature-gated. | cross-run claims/concepts, siblings, own run, knowledge |
| **Harmonic indexing** (Memora) | Indexes by a short *abstraction* + cue *anchors*; consolidates near-duplicates at build time and expands retrieval through anchor links at query time. LLM-optional (degrades to lexical). | knowledge, cases, lessons |
| **Consolidation / hygiene** (D2) | Merges duplicate lessons into an `evidence_count`, preserves bounded cross-run `evidence_refs` plus traceable/untraceable source counts, retires contradicted verdicts, and bounds the store size. Dedup identity is `(statement, task, role)` — a Researcher and a Developer lesson with the same statement never collapse. On top of exact-normalized dedup, a **hybrid-retrieval (grep+BM25+vector, RRF) → agentic paraphrase-merge** pass (per `(task, role)`) folds re-worded duplicates. | lessons |
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

## Deleting a run does not delete what it taught the lab

Run deletion removes the run directory — events, experiments, traces, chat, reports. It does **not**
touch cross-run memory: the lessons, cases, notes, claims and concept capsules that run contributed
live in `LOOPLAB_MEMORY_DIR`, and the whole point of that store is that it outlives the run. The
knowledge base (`LOOPLAB_KNOWLEDGE_DIR`) is human-authored and is never involved.

The cost of that default is provenance: memory rows carry the `run_id` that wrote them, so after the
run is gone the lesson still applies but nothing can be traced back to the experiments behind it, and
the [concept shelf](#the-concept-shelf-memory-indexed-by-the-concept-tree) can no longer give those
rows their run-level tags.

So the Delete dialog offers an opt-in cascade — *“Also delete this run’s own cross-run memory”* —
under one rule: **delete only what is attributable to this run alone.** These stores merge, and the
obvious implementation would be wrong. A consolidated lesson keeps the newest contributor's `run_id`
while carrying other runs' support in `evidence_count`/`evidence_refs`; deleting it on the strength
of that `run_id` would destroy corroboration earned by runs that still exist. `serve/memory_cascade.py`
states one predicate per store, and everything that fails a predicate is kept **and counted with a
reason** shown in the dialog before you agree:

| Store | Deleted when | Kept when |
|---|---|---|
| `lessons.jsonl` | this run wrote it and nothing merged into it | `evidence_count > 1`, lineage naming another run, or untraceable support |
| `meta_notes.jsonl` | this run wrote it | — |
| `cases.jsonl` | this run wrote it (the group's `active` champion is re-elected over what survives) | — |
| `research_claims.jsonl` | this run wrote it | another run's curation decision was computed over that claim pool |
| `concept_capsules.jsonl` | this run wrote it | any of its concepts was merged into a shared concept family |
| `skills/` | never | an auto-skill is promoted only across two differently-fingerprinted tasks, so it is cross-run by construction |
| `*_curation_log.jsonl` | never | append-only governance audit |

The purge runs **after** the run is durably gone, never before: a deletion that then refuses would
otherwise have destroyed the evidence of a run you still have. It is idempotent (“remove every row
attributable solely to R”), so a store that was locked at the moment of deletion can be finished
later — the notice offers a retry, and `POST /api/runs/{run_id}/memory-purge` finishes it after the
run is gone. That endpoint needs the run's **identity in the body**, not just its id: once the run
directory is deleted neither `run_uid` nor `memory_dir` can be read back, and a bare run id names a
directory *name* that the next run reuses. Pass both from the deletion receipt's `memory` block —
an empty body is refused `400 memory_purge_identity_required`, any other key is
`400 invalid_memory_purge_request`, and on a run that still exists a `run_uid` disagreeing with the
run's own record is `409 memory_purge_identity_changed`. The `memory_dir` must also be a store this
server manages (its own, or one a surviving run names); it is not a free-form path, because the
purge rewrites whatever it is pointed at. `GET /api/runs/{run_id}/memory-attribution` is the
read-only preview the dialog is drawn from.

### When the cascade could not have matched anything, it says so

The cascade keys on `run_uid`, and a run started **before 2026-08-11** does not have one — uid
stamping landed in `orchestrator.py`'s `run_started` that day. That is normally harmless, because
such a run's memory rows have no uid either and name matching is the only identity either side has.

It stops being harmless in a **mixed-generation store**, which every shared `memory_dir` became the
day stamping landed. A uid-less run deleted against a store whose rows all carry a uid matches
nothing at all: `RunIdentity.owns` takes its `run_uid` branch, which requires the caller's uid, and
returns false without ever comparing a name. The purge then deleted nothing, kept nothing, and
reported `{"ok": true, "deleted": 0, "kept": 0, "identity": "run_id"}` — a clean success claiming a
keying that never happened, and indistinguishable from "this run contributed nothing".

Two fields now separate those cases. `unmatchable` counts the rows this caller could not match
either way — held apart from `kept`, because a row nobody could form an opinion about is not a
judgement a rule made — and `advisory` is the sentence explaining it. Both appear on
`memory-attribution` and on the `memory` block of a deletion receipt. Alongside them, the parked
identity sidecar records **why** a uid is missing rather than an empty string: `run_uid_source` is
`run_started`, `pre_uid_run` (the log has a `run_started` carrying no uid), `no_run_started`, or
`unreadable` — and only the last is a case where a name keying might be hitting a run that does have
a uid.

### Rows whose run was never deleted through the UI

A cascade only ever runs as part of a deletion. Runs removed **outside** the UI — a `rm -rf`, a
temp directory, a worktree that was thrown away — leave their rows behind with no deletion to hang a
cascade off, and nothing collects them. That is the usual reason a store fills up with rows from
runs that no longer exist, and it is not a cascade failure.

`looplab memory-orphans <memory_dir> --runs-root runs` is the deliberate sweep for it. It reports
every row whose run is gone, grouped by the run that wrote it, and **writes nothing without
`--apply`**. Nothing runs it automatically: these stores are shared and the purge is irreversible.

Two properties make it safe to point at a live store:

* **A surviving run's rows are never proposed.** Attribution is the cascade's own — by `run_uid`
  when the row has one, by `run_id` only when it does not — so a uid-less row whose directory name
  still exists is never a candidate, being indistinguishable from that live run's own legacy row.
* **Removal goes back through the tier predicates**, once per contributing run, so a consolidated
  lesson, a merged-concept capsule and a claim another run's curation was computed over all survive
  their writer. The writing run being gone does not make its contribution to a *surviving* row
  somebody else's to discard.

If any surviving run's event log cannot be read the survey reports `blind` and refuses to call a
uid-carrying row orphaned at all — an unknown uid must never read as an absent one.

### Reaping the service files a finished deletion leaves behind

Every whole-run deletion parks a receipt and an identity sidecar in the run root and takes a
lifecycle lock. `run_projections.py` hides them from the run list, and until now nothing removed
one, so they accumulated for the life of the deployment.

`looplab reap-service-files runs` reports what would go and the rule that decided each file;
`--apply` removes it, `--grace-hours` sets how cold a file must be (24 h by default). The refusals
are the design (`serve/service_reaper.py`):

| File | Removed when | Never removed when |
|---|---|---|
| `.looplab-delete-receipt-*` | the deletion **succeeded** and the receipt is cold | any non-succeeded status (a retry *resumes* from it), or `quarantine_ambiguous` at **any** age — an absorbing state whose receipt is the only record that a human still owes the run a look |
| `.looplab-delete-identity-*` | its receipt is being removed; or no receipt was ever published and it is cold | its receipt is being kept — the pair is never split |
| `.looplab-lifecycle-*.lock` | its digest matches no surviving run directory and it is cold | the run it fences still exists — `flock` is per-inode, so unlinking a held lock silently lets two processes hold it at once |
| `.looplab-reset-receipt-*` | the reset **succeeded** and the receipt is cold | the reset is unfinished and re-enterable |
| `.looplab-delete-fence-*` | never | it is live ownership of a run identity |
| `.looplab-delete-quarantine-*` | never | it holds the run's own bytes |

The grace period is what keeps a *succeeded* receipt answering a retry idempotently instead of
turning the operator's second click into a `404` about a run they deliberately deleted.

Note that `save_deletion_identity` runs **before** the transaction can refuse, so a deletion that is
refused — a run that is not quiescent, say — still leaves a sidecar, and re-pressing the button
leaves another. Those unpaired sidecars are collected by the same sweep once cold.

## Configuration

- `LOOPLAB_MEMORY_DIR` — cross-run memory home (default `~/.looplab/memory`; `""` disables).
- `LOOPLAB_KNOWLEDGE_DIR` — knowledge base home (default `~/.looplab/knowledge`; `""` disables).
- `LOOPLAB_MEMORA` — harmonic indexing (abstraction+anchors) over the stores; **on by default**, set `=0`/`false` to restore the raw-text index.
- `LOOPLAB_RESEARCHER_TOOLS` — master switch for the tool-using Researcher (agentic retrieval); off → a plain researcher that only sees the injected memory.

The assistant can grow the knowledge base directly: share experiment results/lessons and ask it to
remember them, and it distils + saves a note via its `remember` tool. The tool is unavailable in
Plan mode and remains subject to the active mode's write permissions in mutating modes. See
[LLM & coding agents](llm-and-agents.md).
