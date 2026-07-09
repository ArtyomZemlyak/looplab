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
| **Cases** (`cases.jsonl`) | *The list of the best* — the winning config per task, verbatim, retain-on-improvement. | `{task_id, goal, params, metric, operator, rationale, run_id, evidence}` | cross-run | run-end | `kb_search`, exact warm-start |
| **Meta-notes** (`meta_notes.jsonl`) | *Why it won* — a short, LLM-distilled **causal** summary of what actually mattered (not the raw config — that's the case). | `{task_id, note}` (causal prose) | cross-run (per task) | run-end (LLM; falls back to a stats line) | exact warm-start, `recall_notes` |
| **Lessons** (`lessons.jsonl`) | *Generalizable good **and** bad findings* — higher-level claims ("larger batch tends to help") with a verdict and how many observations back it. | `{statement, outcome: supported/tested/abandoned/failed/refuted, delta, confidence, evidence, evidence_count, fingerprint}` | cross-run (task-fingerprint matched) | run-end (from resolved hypotheses + winner + failure themes) | prompt injection (fingerprint-matched), `search_lessons` |
| **Developer lessons** (`dev_lessons.jsonl`) | *How to **build** it* — IMPLEMENTATION gotchas + techniques (dataset-loading traps, framework/version API quirks, build/train pitfalls, orchestration that worked). Distinct from Lessons, which are about **which** experiment to run. | `{statement, outcome: technique/pitfall, confidence, evidence_count, fingerprint, source: developer/distilled}` | cross-run (task-fingerprint matched) | Developer self-authors mid-session (`remember_dev_lesson`) + run-end engine distillation | top-5 **preview** injected into the Developer prompt, `search_dev_lessons`/`list_dev_lessons` |
| **Skills** (`skills/*.md`) | *Best practices **with the script*** — a reusable technique + the code that implemented it; promoted when it re-confirms on a different task. | markdown: `name, description, status (candidate/promoted), fingerprints`, evidence, an **`### Implementation` code block** | cross-run (generalizes across tasks) | run-end (supported hypothesis, Δ>0) | `list_skills`, `use_skill` |
| **Knowledge base** (`knowledge/*.md`) | *Anything worth keeping* — free-form notes, hand- or agent-authored (the assistant's `remember` tool). | markdown notes | cross-run | assistant `remember`, by hand | `kb_search`, `list_notes`, `read_note` |
| **Hypotheses** (ledger, in-run) | *What's worth testing* — accepted "to-test" beliefs with a live **status** (open → testing → supported/tested/abandoned) and accumulating evidence. Deduped by exact hash **plus** an agentic paraphrase-merge (`hypothesis_merged` — the engine decides, the fold applies it deterministically, cadence open≥4 & grew≥2); the open board is prioritized by **foresight**. | `{statement, status, evidence, best_delta, source}` per node | one run (derived from the event log) | Researcher (`idea.hypothesis`) + `hypothesis_added` | injected into the proposal prompt |
| **Research memo** (deep-research) | *Breadth of scope* — a hard-thinking pass over all results + the web; its `recommended_directions` can **become hypotheses**. | `{summary, findings, claims:[{statement, node_ids, urls}], recommended_directions}` | one run | on cadence / manual / strategist | folded into `RunState.research`, verified by the D8 verifier |
| **Exploits** (`exploits.jsonl`) | *Defensive memory* — patterns of cheating/leakage the trust layer scans for (co-evolved hacker-fixer). | `{name, pattern, kind}` | cross-run | `looplab harden` | reward-hack scan at eval |

In-run **working memory** (rebuilt from the event log each turn, never persisted separately): the
**hypotheses ledger**, the **diversity archive** (MAP-Elites elites/niches), and the **digests** —
`experiments_digest` (winners + failures + sweep landscape), `sibling_digest` (what siblings already
tried), `lineage_lessons` (subtree outcomes ranked by |Δ|), `ancestral_repair_chain` (prior repairs).
The single source of truth for everything in-run is the append-only `events.jsonl`.

## Methodologies (how memory moves)

| Methodology | What it does | Touches |
|---|---|---|
| **Reflection / distillation** (run-end) | Distils the run into cross-run memory: the winner → a case; *why* it won → a causal meta-note; resolved hypotheses + failure themes → lessons; a supported technique + its code → a skill. | cases, meta-notes, lessons, skills |
| **Task fingerprint + similarity** (M2) | A deterministic task descriptor (kind, direction, metric, goal keywords, param names); Jaccard overlap gates/ranks cross-run transfer to *similar* (not just identical) tasks. | lessons, skills |
| **Passive prompt-injection** (run-start + per-proposal) | Fingerprint-matched lessons + exact-task meta-notes + the always-on digests + open hypotheses are written into the proposal prompt; contradicted verdicts are quarantined (newest wins). | lessons, meta-notes, hypotheses, digests |
| **Active agentic retrieval** | The Researcher *calls tools* to pull memory when it wants (see below). | all cross-run types + siblings + own run |
| **Harmonic indexing** (Memora) | Indexes by a short *abstraction* + cue *anchors*; consolidates near-duplicates at build time and expands retrieval through anchor links at query time. LLM-optional (degrades to lexical). | knowledge, cases, lessons |
| **Consolidation / hygiene** (D2) | Merges duplicate lessons into an `evidence_count`, retires contradicted verdicts, and bounds the store size. On top of exact-normalized dedup, a **hybrid-retrieval (grep+BM25+vector, RRF) → agentic paraphrase-merge** pass lets the Researcher fold re-worded duplicates. | lessons |
| **Verification** (D8) | The evidence ledger — each research claim carries its citing `node_ids`/URLs so a verifier can check it (audit-only). | research memo |

## Agentic retrieval — the agent can pull *anything*

The tool-using Researcher can actively reach **every** memory type, so it's never limited to what was
auto-injected:

| Memory | Tool(s) |
|---|---|
| Knowledge base + cases | `kb_search`, `list_notes`, `read_note` |
| Lessons | `search_lessons` — returns each claim's verdict + "verified across N observations" |
| Meta-notes | `recall_notes` — causal summaries for this/similar tasks |
| Skills | `list_skills`, `use_skill` |
| Own experiments | `list_experiments`, `read_experiment`, `read_code` |
| Sibling runs (same task) | `list_sibling_runs`, `read_sibling_experiment`, `read_sibling_code`, `find_analogous_across_runs` |
| Hypotheses | injected each proposal (open ones, with instruction to reuse exact wording for evidence linking) |

## Developer memory — a separate stack for the *builder*

Everything above serves the **Researcher** (which experiment to run and why). The **Developer** — the
agent that turns an idea into runnable code on a repo — has its own two-part memory so it never has to
relearn how to *build* on a given kind of repo:

- **Read the search history on demand** (`developer_run_tools`, on by default): the Developer gets the
  SAME read-only run tools the Researcher has — `read_experiment` / `read_code` / `read_logs` /
  `find_analogous` over its own run, and (gated by `cross_run_tools`/`all_runs_tools`) `read_run_code` /
  `read_run_experiment` over any run on the machine. `read_code` returns a node's **final evaluated**
  code and **flags a failed node** as the version that broke. Use it when the idea builds on a prior
  node, when a merge lists several parents, or on a repair.
- **Developer lessons** (`developer_memory`, on by default; `dev_lessons.jsonl`): a separate cross-run
  store of IMPLEMENTATION lessons. The Developer **self-authors** them mid-session with
  `remember_dev_lesson` ("this repo's dataset only loads with its own pickle loader"), **searches** them
  with `search_dev_lessons` / `list_dev_lessons`, and sees a compact **top-5 preview** (fingerprint-
  matched, one line each) up front — the full text stays behind the tool, not dumped into the prompt.
  The engine also distils a few implementation lessons at run end (LLM-generalized from the build/repair
  history, deterministic failure-class fallback offline). They surface in the UI under the **Dev
  lessons** tab of the Memory panel, alongside the Researcher's lessons.

The Developer's read tools appear in its agent trace like any other tool call, so what it read/wrote is
auditable per node.

## Configuration

- `LOOPLAB_MEMORY_DIR` — cross-run memory home (default `~/.looplab/memory`; `""` disables).
- `LOOPLAB_KNOWLEDGE_DIR` — knowledge base home (default `~/.looplab/knowledge`; `""` disables).
- `LOOPLAB_MEMORA` — harmonic indexing (abstraction+anchors) over the stores; **on by default**, set `=0`/`false` to restore the raw-text index.
- `LOOPLAB_RESEARCHER_TOOLS` — master switch for the tool-using Researcher (agentic retrieval); off → a plain researcher that only sees the injected memory.
- `LOOPLAB_DEVELOPER_RUN_TOOLS` — give the Developer the read-only run tools too (own run always; sibling/all-runs gated by the cross-run toggles); **on by default**.
- `LOOPLAB_DEVELOPER_MEMORY` — the Developer's implementation-lessons store (`dev_lessons.jsonl`); **on by default** (needs a memory dir).

The assistant can grow the knowledge base directly: share experiment results/lessons and ask it to
remember them, and it distils + saves a note via its `remember` tool (works in any mode). See
[LLM & coding agents](llm-and-agents.md).
