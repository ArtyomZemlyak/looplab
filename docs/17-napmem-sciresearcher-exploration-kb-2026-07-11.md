# LoopLab — Arch-design study: Frameworks/Libs KB · NapMem · SciResearcher · Narrow-Exploration (2026-07-11)

**Companion docs:** [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) (ADR-6, ADR-9, ADR-10, ADR-16) · [guide/memory.md](guide/memory.md) · [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md) (same house style)

**Method.** Two parallel code-level maps of the subsystems these ideas touch — the knowledge/memory
stack (`tools/memora.py`, `tools/vectorstore.py`, `tools/knowledge_tools.py`, `tools/memory_tools.py`,
`engine/memory.py` + `engine/lessons*.py`, `search/hybrid_merge.py`, `tools/env_inspect.py`) and the
research/exploration stack (`agents/roles.py`, `agents/agent.py`, `engine/proposal_cues.py`,
`engine/novelty.py`, `search/archive.py`, `search/coverage.py`, `search/policy.py`, `search/foresight.py`,
`agents/deep_research.py`, `engine/research_cadence.py`, `engine/genesis.py`, `agents/strategist.py` +
`engine/strategy.py`) — plus primary-source research on the three papers. **arXiv full texts were
proxy-blocked this session** (403 on `arxiv.org/abs|html|pdf`, as in doc 13); paper claims come from
search-index snippets + secondary reviews and are marked where unverified. Every code claim below is
anchored to `file:line` from a direct read. Sources are linked at the end.

**The four items.** Work through, at the arch-design + code-integration level: (0) a **general knowledge
base** of frameworks + libraries (общая БЗ); and three papers — (1) **NapMem** (navigable memory
pyramid), (2) **SciResearcher** (scaling deep-research agents), (3) **AI Research Agents Narrow Scientific
Exploration**. For each: how it integrates in code, the complications, and whether there is synergy.

**TL;DR verdict.**

| Item | Verdict | Synergy | Effort | Mode |
|---|---|---|---|---|
| **0 · Frameworks/Libs KB** | **Build it** — the missing *curated semantic corpus*; the store + retrieval tools exist, but note frontmatter/versioning is *not* implemented yet, so it's a small real build, not "drop files in." | **High** — substrate the other three read from. | **S–M** | all |
| **1 · NapMem** | **Adopt the structure, drop the RL** — retrieval today is **flat top-k everywhere**; NapMem is the navigable-pyramid upgrade of ADR-10. A **dormant `CaseLibrary`** is a ready-made seam. | **High** — closest fit; an ADR-10 revision. | **M** | large-corpus / open-ended |
| **2 · SciResearcher** | **Backend option + deep-research patterns, not a framework** — it's a *training + data-construction* paradigm yielding an 8B bio/chem model; we're inference-time, backend-agnostic, ML-engineering. | **Modest** — the concrete win is turning on literature/web in `deep_research`. | **S (backend/flags) / L (training)** | opt-in |
| **3 · Narrow-Exploration** | **Partly already built** — `coverage.py` cites this exact paper and computes a concentration signal, but it's *context-only, reactive*. Make it proactive + add distance-from-seed + wire it to selection. | **High** (open-ended) / N/A (fixed-metric) | **M** | open-ended only |

Nothing here replaces the core. Three of four (1, 3, and the KPI half of 3) are upgrades to machinery
already in the tree; item 0 is a small build on the existing store; the only genuinely new capability class
is training our own model (SciResearcher angle C), which is the one thing that breaks backend-agnosticism —
so it stays opt-in and external.

---

## 0. The general frameworks & libraries knowledge base (общая БЗ)

### What we have vs what's missing (code-verified)

The **storage + retrieval** exist; the **curated corpus** and the **rich note schema** do not.

- **`knowledge/*.md`** — free-form notes, canonical on disk (`~/.looplab/knowledge`, `LOOPLAB_KNOWLEDGE_DIR`).
  Read via `kb_search`/`grep`/`list_notes`/`read_note` (`KnowledgeTools`, `knowledge_tools.py:192-319`);
  written by the assistant's `remember` tool (`KnowledgeWriteTools`, `knowledge_tools.py:137-189`). Sample
  notes are ML *concepts* (`examples/knowledge/polynomial_model_selection.md`).
- **Skills** — the **procedural** tier (`SkillTools`, `skills.py:56-89`; `examples/skills/cross_validation.md`):
  a recipe + code, read via `list_skills`/`use_skill`. **Skills already carry YAML frontmatter**
  (`name/description/status/provenance/fingerprints`, written by `write_auto_skill`, `memory.py:411-446`;
  parsed by `_parse_skill`, `skills.py:22-35`).
- **`tools/env_inspect.py`** — the repo Developer's **live, read-only** introspector: an installed package's
  *version/source*, a class/function *signature*, an Enum's valid members, grep over installed source. Built
  to kill the #1 repo-experiment failure — the Developer **guessing** an API and being wrong (`precision='16-mixed'`
  vs `'16'`, a nonexistent `--gradient_clip_val`, an import that moved between versions; `env_inspect.py:1-9`).

**Two gaps, both real:**

1. **No curated framework/library corpus.** The idioms, gotchas, version-sensitive APIs, and "reach for X
   when Y" wisdom for the ML stack the agents actually write against (PyTorch/Lightning, JAX/Flax,
   scikit-learn, XGBoost/LightGBM/CatBoost, HF Transformers/`timm`, Optuna, pandas/Polars, …) live only in the
   base model's weights (stale, version-blind) plus whatever `env_inspect` reads off the *installed* package.
   `env_inspect` gives **live truth but no wisdom** ("what the API is" — not "this optimizer diverges without
   LR-warmup", "this splitter leaks on grouped data", "on this GPU prefer bf16").
2. **The rich note schema is docs-only.** ADR-10/ADR-16 describe notes as `{content, frontmatter(provenance,
   type, task_fingerprint, confidence, status), embedding, tags, [[links]]}` (`02-architecture.md:213`,
   `03-decisions.md:280-283`) — but **no code produces it.** `KnowledgeWriteTools.execute`
   (`knowledge_tools.py:163-189`) writes plain markdown + a trailing `_tags:_` line: **no frontmatter, no
   provenance/type/confidence/status, no on-disk embedding** (embeddings are ephemeral, rebuilt in-memory by
   `KnowledgeTools._build_index`, `knowledge_tools.py:233-265`), **no `[[links]]`** (wikilink-graph is
   unimplemented — see item 1). The structured fields (`fingerprint/confidence/outcome`) exist only in the
   **JSONL** stores (`lessons.jsonl`/`cases.jsonl`), not in knowledge notes.

### Design — the KB is the ADR-10 *semantic* tier, split by facet

```
knowledge/
  frameworks/<name>.md   # pytorch-lightning.md, jax.md, xgboost.md — capabilities, idioms, when-to-use
  libs/<name>.md         # optuna.md, polars.md, timm.md          — version-sensitive APIs, gotchas, pins
  seed/<topic>.md        # existing ML-concept notes (unchanged)
  index/                 # DERIVED (vector), rebuildable          (ADR-10)
```

Two facets on purpose:
- **Frameworks** (torch/lightning/jax/sklearn/xgboost/hf/…): capability map, idiomatic usage, failure modes,
  and a `[[link]]` (once links exist) to the matching **Skill** where one exists.
- **Libs** (optuna/polars/timm/`accelerate`/…): the version-sensitive surface — API shapes that changed
  across versions, dtype/device gotchas, pins that matter. This is exactly what pairs with `env_inspect`:
  the **note says what to watch for; `env_inspect` confirms what's installed.**

### Code seams (corrected — this is a small build, not zero-code)

| Concern | Seam | Change |
|---|---|---|
| Storage/format | `knowledge/{frameworks,libs}/*.md` | New dirs — no code |
| **Note frontmatter** (version_range, type, confidence) | `KnowledgeWriteTools`/`KnowledgeTools` (`knowledge_tools.py:163-189, 220-265`) | **Real, small change**: add YAML-frontmatter parse/emit — **borrow the Skills machinery** (`_parse_skill`, `write_auto_skill`) which already does exactly this |
| Indexing/retrieval | `KnowledgeTools._build_index`/`_records` (`knowledge_tools.py:220-265`) | Reuse; add a `type: framework\|lib` tag so the router can prefer it on Developer queries; optionally a per-facet index (ADR-10 "separate indices") |
| Live-truth companion | `tools/env_inspect.py` | Unchanged; **document the pairing** in the Developer prompt (curated note ↔ live introspection) |
| Agent access | `kb_search`/`read_note` (+ `use_skill`) | Already exposed (`kb_search` = flat top-k=3 + 1 anchor hop, `knowledge_tools.py:282-300`); add a Developer hint to consult `frameworks/`/`libs/` before writing framework code |
| Persistence | `InMemoryVectorStore` (`vectorstore.py:165-201`) | Today the KB re-embeds every run (no persistent store ships; LanceDB is a design-only seam, `vectorstore.py:1-9`). Fine at small corpus; note it if the KB grows |

### Complications

- **Staleness & version-sensitivity.** A note about torch 2.3 is wrong on 2.7. Add a `version_range`
  frontmatter field; prefer `env_inspect`'s *live* version and down-weight out-of-range notes. **Never let a
  curated note override live introspection** — `env_inspect` is truth.
- **Context-rot / distractors.** ADR-10 point 2: *do not merge curated knowledge with distractor-rich
  ingested RAG in one index.* Today everything flattens into one "kb" index (`knowledge_tools.py:233-265`) —
  keep `frameworks/`/`libs/` curated-tagged and separable.
- **Curation cost & poisoning.** A wrong framework note *confidently* misleads the Developer — worse than
  none. Reuse ADR-10 gating (confidence, `candidate→trusted`, mark-invalid ledger) and the poisoning filter.
  Skills' `status: candidate|promoted` frontmatter is the pattern to copy.
- **Scope creep.** This is *curated guidance*, not a docs mirror. Keep notes short (guidance lives in the
  note, code in the Skill, API in `env_inspect`); past ~200k tokens the ADR-3 "skip RAG, load in-context"
  heuristic flips.

### Synergy

**The substrate the other three read from** — the ADR-10 semantic tier finally populated for the *tools of
the trade*. It pairs with `env_inspect` (curated wisdom × live truth), with Skills (guidance × runnable
recipe), and it is exactly the corpus NapMem (item 1) navigates and that a broadened Researcher (item 3)
needs to propose *cross-framework* ideas instead of staying in one library's rut.

---

## 1. NapMem — navigable memory pyramid (arXiv 2607.05794, Jul 2026)

**What it is** *(snippet + secondary-review sourced)*. "From Passive Retrieval to Active Memory Navigation:
Learning to Use Memory as a Structured Action Space." It reframes long-term memory from **flat top-k
retrieval** into a **linked multi-granularity pyramid**: **raw conversations** (evidence) → **typed memory
records** (compact facts/preferences) → **topic tracks** (cross-session aggregation) → **user profiles**
(stable summaries), connected by **provenance relations** (each level links *down* to the evidence it was
distilled from). Each level is a **granularity-specific tool**; the agent is **RL-trained (GRPO)** to choose
which tool given the query + evidence so far — start broad, **drill down**, **stop when enough** — rewarded
for *accurate answer + valid format + appropriate memory use under a tool-call budget*. RL **reduces
unnecessary calls** and *calibrates* use (not "retrieve more"). Competitive on **PersonaMem-v2, LongMemEval,
LoCoMo**.

### Relation to LoopLab — retrieval is flat top-k almost everywhere (code-verified)

The subagent map is unambiguous: **there is no tiering, no coarse→fine navigation, no pyramid today.** The
only two non-flat behaviors are (a) Memora's *single lateral anchor hop* and (b) Skills' manifest→body
disclosure:

- **`kb_search`** — flat **top-k=3** vector hits + one round of anchor-expansion appended as
  `[related via anchors]` (`knowledge_tools.py:282-300`; k defaults to 3, never overridden).
- **`search_lessons` / `recall_notes`** — **pure token-overlap set intersection, no embeddings at all**,
  top-`limit` (`memory_tools.py:68-100`).
- **Memora** (`tools/memora.py`) — indexes an `Abstraction` = `primary` (essence) + `anchors` (cue tags);
  `expand_by_anchors` (`memora.py:241-269`) is **one extra retrieval hop** to "different-primary, shared-cue"
  entries — *lateral cross-links, not a hierarchy* (`Abstraction` has no level field).
- **`retrieve_lessons_harmonic`** (`memory.py:229-278`) builds a *fresh flat index per call*, top-k + one
  anchor hop; `_render_role_prior` then Jaccard-gates and picks **top-5** (`lessons_priors.py:160-173`).
- **`VectorStore`**: only `InMemoryVectorStore` ships (brute-force cosine, `vectorstore.py:165-201`); **no
  BM25/hybrid here** (RRF lives in `hybrid_merge.py`, and is a *write-path hygiene* tool, not read retrieval).
- **`[[wikilinks]]→graph`: confirmed NOT implemented** — no `[[`-parsing, no `networkx` in the memory code
  (`networkx` appears only as a dep string in `runtime/deps.py:67`). GraphRAG is a deferred ADR-16 seam.

But the *tiers* NapMem wants **already exist as separate stores** — they're just not linked or navigable:
**cases** (winning config, verbatim = evidence) → **meta-notes** (*why* it won, per task) → **lessons**
(generalizable claims) → **skills** (promoted recipe). That is raw-evidence → typed-record → topic-track →
profile, in our vocabulary (`guide/memory.md`). And **ADR-10 point 4 already mandates progressive
disclosure** (manifest → note → detail) — NapMem just makes it an *agent-driven, provenance-linked* action.

### The ready-made seam — a dormant `CaseLibrary`

There is a **`CaseLibrary`** class (`memory.py:514-609`) — VectorStore-backed, with anchor-expanding
`retrieve` (`:578-585`), build-time `_consolidate` of near-duplicates (`:545-576`), and `retain_if_improved`
(`:587-609`) — **defined but never instantiated in production** (the wired one is `JsonlCaseLibrary`, a flat
keyword top-k, `:449-511`). This dormant class is already 80% of a pyramid *tier*: activate/generalize it into
level-aware tiers rather than writing a new store.

### Integration seams

| NapMem piece | LoopLab seam | Change |
|---|---|---|
| Multi-granularity pyramid | the 4 stores (cases/meta-notes/lessons/skills, `engine/memory.py` + `lessons*.py`) | Add **typed provenance edges** case→meta-note→lesson→skill (they exist implicitly at distillation; make them explicit `[[links]]`/anchors) |
| Provenance-linked navigation | `tools/memora.py` `expand_by_anchors` (`:241-269`) | Generalize the 2-level anchor hop into an N-level typed pyramid; anchors → typed provenance edges |
| Level-aware store | dormant `CaseLibrary` (`memory.py:514-609`) + `VectorStore` protocol (`vectorstore.py:37-41`) | Activate/extend it; a summary/manifest index per tier |
| Granularity-specific tools | `KnowledgeTools`/`MemoryTools` tool set | Add drill-down tools: `summary_of(topic)` → `open_note(id)` → `evidence_for(id)`; the tool-using Researcher already picks tools (`agent.py:213-267`) |
| "Appropriate use under a budget" | tool-loop context/cost budget (`context_budget.py`, per-role caps) | Map NapMem's tool-call budget onto our existing budget — **no RL** |

### The RL question — skip it

NapMem's headline mechanism (GRPO-train the model to navigate) **does not fit LoopLab** (inference-time,
backend-agnostic; a PersonaMem-trained policy wouldn't transfer to ML-research memory). **Adopt the
navigable-structure half** — pyramid + provenance tools + drill-down — and let the existing tool-using
Researcher navigate by prompt-guided function-calling. Keep NapMem's *insight* ("memory use is an explicit,
budgeted decision; stop when you have enough") as prompt guidance + the existing budget guard, not a trained
policy. (If we ever fine-tune a local role — item 2 angle C — the navigation trajectories become natural
training data, but that's opt-in and external.)

### Complications

- **No RL** = structure without *learned* calibration; navigation quality rides on the base model's tool-use
  judgment. Degrades to today's behavior, only better-structured.
- **Provenance-graph construction cost.** Building typed edges at run-end distillation adds work to
  `engine/memory.py`'s reflection; edges must be derived/rebuildable (like the vector index) and replay-safe.
- **More tool-calls = more latency/cost.** Drill-down is several round-trips vs one top-k — pays only when
  the corpus is large enough that flat top-k pulls distractors (the context-rot case). Gate it; keep flat
  top-k as the cheap default.
- **No persistent store yet.** With only `InMemoryVectorStore`, a large pyramid re-embeds each run — the
  LanceDB seam (`vectorstore.py:1-9`) becomes worth building *before* a big pyramid, not after.

### Synergy — highest of the three

NapMem is **almost an ADR-10 revision**: the *navigable* upgrade of the exact tiering + progressive
disclosure + Memora anchors we already committed to, and it directly attacks the **context-rot** failure
ADR-10 exists to avoid. It reads the item-0 KB as one more tier. **Recommend: fold NapMem into ADR-10 as the
retrieval-interface upgrade; activate the dormant `CaseLibrary` and ship the pyramid tools first; defer any
RL indefinitely.**

---

## 2. SciResearcher — scaling deep-research agents (arXiv 2605.01489, May 2026)

**What it is** *(snippet + Moonlight-review sourced)*. Zheng, Wang, Li, Song, Fang. A **fully automated
agentic *data-construction* framework** for frontier science: synthesizes diverse **conceptual + computational**
tasks grounded in academic evidence, eliciting *information acquisition, tool-integrated reasoning, and
long-horizon* capabilities. It trains **SciResearcher-8B** via **cold-start SFT** on agent trajectories from a
**Claude-Sonnet-4.5 teacher** with **rejection sampling**, then **RL**. Results: **19.46% on HLE-Bio/Chem-Gold**
(SOTA at 8B, beating larger proprietary agents), **+13–15%** on SuperGPQA-Hard-Biology and TRQA-Literature.
The contribution is a **paradigm for automated training-data construction** — not a runnable framework.

### Relation to LoopLab — a model + a data pipeline, not a framework

Three reasons it's **not** a drop-in: it yields a **trained 8B model + a data-synthesis pipeline**, not an
orchestrator (we're backend-agnostic, ADR-7 — we don't ship/train a model); its **domain is bio/chem
reasoning**, not ML-engineering on a metric harness; and its "scaling" is **training-data scaling**, not
test-time search scaling (which for us is ADR-6's throughput lever).

### Integration angles, cheapest first — and the concrete win

- **(A) Backend/model option — S, config-only.** *If* SciResearcher-8B ships under a usable license, wire it
  as a cheap **local** backend for the deep-research pass via LiteLLM (`roles.*.model`, ADR-7/§9). Caveat:
  bio/chem tuning may not transfer to ML ideation — validate before trusting; likely a *deep-research/grounding*
  backend, not the MLE Researcher.
- **(B) Turn deep research from introspective to literature-expanding — S, the real win.** The subagent map
  found the decisive gap: `deep_research` **defaults `web_search=False` and `literature_search=False`**
  (`config.py:691,695`); `LiteratureTools`/`WebTools` are only wired when on (`deep_research.py:214-256`), and
  the foresight ranker's tools never include web at all (`cli/__init__.py:169-178`). So **by default the
  research memo is grounded in the run's own experiments + local knowledge — not external literature.** That is
  the *opposite* of SciResearcher's "information acquisition" pillar. The single highest-ROI SciResearcher-
  flavored change is: **default literature/web on for the deep-research pass** and give it a **broader
  tool-integrated reasoning budget** (`max_turns`/`emit_after`, `deep_research.py:120-121`). The plumbing to
  *act* on the output already exists — `recommended_directions` auto-become OPEN hypotheses
  (`research_cadence.py:130-136`). Seam: `make_deep_researcher` (`deep_research.py:214`) + the two default
  flags.
- **(C) Self-distillation from our own `events.jsonl` — L, opt-in, out of core scope.** The teacher→
  trajectory→rejection-sampling→SFT recipe *could* fine-tune a cheap local role from **LoopLab's own** event
  log — we already record every proposal/patch/verdict, a trajectory corpus with **verified rewards from the
  trust layer** (exactly the signal rejection-sampling needs). Genuinely synergistic on paper, but a **training
  project**, not an inference-engine feature; it also risks the "recursively train on un-curated self-output"
  trap ADR-10 point 3 warns against (mitigated by our *gated* verdicts, still a hazard). Park as a research
  direction.

### Complications

- **Availability/licensing unknown** (angle A). **Domain transfer** may hurt, not help — A/B on our tasks.
- **Training is out of scope** (angle C): needs a training stack, GPU budget, curation/reward pipeline.
- **Don't delegate the loop** (ADR-7 rule 1): even a strong SciResearcher-8B backs a *step* (the deep-research
  pass), never the research loop.

### Synergy — modest, concentrated at the deep-research stage

Bounded but real: (A)+(B) plug into `agents/deep_research.py` + the ADR-7 backend seam, and (B) is a
genuinely cheap, high-value change (flip two defaults + widen a budget) that makes the literature-grounded
half of LoopLab behave like SciResearcher's information-acquisition pillar. (C) is the tantalizing long-shot —
*we already own the trajectory+reward data such a pipeline needs* — but it's a separate endeavor.
**Recommend: (i) turn on literature/web + widen the deep-research budget now; (ii) benchmark SciResearcher-8B
as a deep-research backend if released; (iii) shelve self-distillation as a research direction.**

---

## 3. AI Research Agents Narrow Scientific Exploration (arXiv 2605.27905, May 2026)

**What it is** *(HF-page + snippet sourced)*. Tang & Yang. An **empirical diagnostic**: 4 AI research-agent
frameworks × 6 LLMs generate **37,802 ideas** from shared seed literature across citation-defined AI/ML areas,
vs human papers from the same areas. **Four consistent patterns:** (1) AI ideas are **substantially more
concentrated** than human papers; (2) they stay **much closer to the seed literature** than human follow-on
work; (3) papers most similar to AI ideas get **lower subsequent citations**; (4) when AI ideas differ, the
difference is mostly **recombining existing methods**, not **new research questions**. **Conclusion: current
agents are better at *local elaboration* than *broadening exploration*.** (Diagnostic — supplies *metrics*,
not a fix; concurrent related work: *Heuresis* 2606.25198 on quality/diversity/novelty search.)

### LoopLab already has this pathology — and already cites this paper

The subagent map found the smoking gun in two places:

- **The narrowing is baked into prompts.** `ToolUsingResearcher`'s system prompt literally says *"Work
  FOCUSED, not scattered: pick the most promising direction... and RESEARCH THAT"* (`agent.py:103-117`), and
  `_state_brief` **always leads with the current best + parent** (`roles.py:307-311`). This is precisely
  finding #4 (local elaboration of the leader) as a design choice.
- **`search/coverage.py` already cites arXiv 2605.27905** in its docstring (`coverage.py:16-19`) and computes
  a concentration signal — `themes`, `niches`, `theme_entropy`, `dominant_theme_frac`, `recent_dominant_frac`
  (`coverage_signal`, `coverage.py:50-100`). **So the metric this paper implies is already implemented** —
  but it is **context, never a decision**: recorded as `coverage_snapshot` sidecars (`strategy.py:239-252`)
  and fed to the Strategist, nothing more.

### The honest tension with ADR-6 — and its resolution

ADR-6 **demoted** the diversity archive + fancy policies as *"unproven on MLE-bench; greedy + good operators
wins."* This paper says agents *systematically narrow*. **Not a conflict — different objectives:**

- **Fixed-metric mode (MLE-bench).** The metric *is* the goal; local elaboration *is* the win. ADR-6 correct.
- **Open-ended mode (Genesis, `deep_research.recommended_directions`, open dataset tasks, cross-run research).**
  No fixed metric; value = genuinely novel directions. **Here the Narrow-Exploration finding bites**, and the
  parked diversity/novelty machinery becomes load-bearing again.

So the paper **re-validates the demoted machinery, scoped to open-ended mode** — a *scoping* of ADR-6, not a
reversal.

### What's built vs missing (code-verified)

| Lever | Status today | Gap |
|---|---|---|
| Concentration metric | **Built** — `coverage_signal` (`coverage.py:50-100`), folded every cadence | Within-run *theme* concentration only; not distance-from-seed-*literature*; **not acted on** except reactively |
| Novelty gate | `_llm_novelty_gate` default (`novelty.py:70-131`) — **within-run dedup** ("already tried in THIS run"), prefers NOVEL only vs repeats; doesn't hard-reject | No notion of "too close to the seed literature"; `"algo"` semantic gate off by default (`config.py:298`) |
| Diversity archive | `DiversityArchive` (`archive.py:12-46`) — **pure bookkeeping**; nothing consumes it for selection | No MAP-Elites "expand an empty niche" operator |
| Selection diversity | Lives in `EvolutionaryPolicy.weighted_parent` / `MCTSPolicy` (`policy.py:133,369`) — **both off by default**; `GreedyTree` always improves `state.best()` (`policy.py:286`) | Default is pure exploitation |
| Broaden lever | Strategist `novelty_stance=explore\|balanced\|exploit` (`strategist.py:50-68`) — **the main dial** | **Reactive**: flips to `explore` only *after* concentration ≥0.6–0.75 (`strategist.py:145-160`), i.e. after collapse; stall logic keys on metric stagnation, blind to coverage collapse (`strategist.py:305-324`) |
| Diverse seeding | Genesis authors *what to solve*, not an idea portfolio (`genesis.py:161-238`); seeds = 3 blind drafts (`policy.py:225-227`) | No "generate N orthogonal seed directions" step |

### Integration seams

| Finding → lever | Seam | Change |
|---|---|---|
| Concentration is measurable | `coverage_signal` (`coverage.py:50-100`) — already computed | **Surface it as a KPI** (dashboard) and **make the Strategist trigger proactive**, not post-collapse |
| Distance-from-seed | `engine/novelty.py` + the seed embeddings from Genesis/ingestion | Add a **distance-from-seed** term (reuse the `_embedder`/`HybridRetriever` plumbing already present); degrade to distance-from-archive on `--no-genesis` |
| Question-novelty vs method-recombination | `engine/novelty.py`, `idea.hypothesis` field | Embed the idea's *question/hypothesis* separately from its *method*; stop scoring pure recombination as "novel" |
| Diversity in selection | `GreedyTree.next_actions` (`policy.py:172-288`) + `DiversityArchive.build()` (`archive.py:20`) / `weighted_parent` (`policy.py:133`) | Reserve a **breadth quota** (every Nth node a forced-divergent draft) or a niche-expansion action — **open-ended mode only** |
| Proactive, not reactive | `_rule_novelty_stance` / Strategist prompt (`strategist.py:145,353`) | Lower/invert the collapse thresholds; add a coverage-collapse stall trigger (today it's metric-only) |
| Broaden at entry | `engine/genesis.py` + the board (`hypothesis_added` + `_prioritize_board`) | A seed-phase "portfolio generation": emit N deliberately-orthogonal seed hypotheses (the board machinery already carries them) |
| Broaden at ideation | `agents/deep_research.py` `recommended_directions` (auto-become hypotheses, `research_cadence.py:130-136`) | Diversify the directions set; compounds with item 2(B)'s literature-on change |

### Complications

- **Mode-gating is mandatory.** Diversity pressure **off** in fixed-metric mode, or it trades MLE-bench score
  for breadth — the exact regression ADR-6 warned about. Gate on task kind / open-ended flag.
- **"Novel" ≠ "good."** Naively maximizing distance-from-seed surfaces low-quality far-out ideas. Pair with the
  foresight quality estimate / trust layer — **quality-diversity** (why MAP-Elites, not random jitter), not
  diversity-at-any-cost. (Finding #3 is about AI ideas being *derivative*, not novelty causing low quality.)
- **Question-vs-method novelty** needs a representation we only *partly* have; the `idea.hypothesis`/method
  split is a feasible first cut, imperfect.
- **Seed anchor availability.** Distance-from-seed-literature needs a seed embedding set (present after
  Genesis/ingestion, weak on bare `--no-genesis`); degrade to distance-from-archive.

### Synergy — high in open-ended mode; and it hands us a KPI already half-built

The most *conceptually* aligned of the three with LoopLab's stated ambition (an autonomous *researcher*, not
just an MLE-bench climber). It (a) **re-justifies** the parked machinery for open-ended mode, (b) hands us a
**concentration KPI that is already computed** — the work is to *act* on it (proactive Strategist trigger +
dashboard) and to add *distance-from-seed*, and (c) composes with item 0 (a broad KB → cross-framework
directions) and item 1 (a navigable pyramid surfaces far-from-seed precedents). **Recommend: make the existing
`coverage_signal` proactive (KPI + collapse trigger) + add distance-from-seed first (cheap, immediately
useful), then re-activate quality-diversity selection behind the open-ended-mode gate.**

---

## 4. Cross-cutting: the four items compose into one story

They stack — substrate → navigation → objective + reasoner:

```
  ┌────────────────────────────────────────────────────────────────────┐
  │  0 · Frameworks/Libs KB  →  the curated SEMANTIC SUBSTRATE          │
  │       (what the tools of the trade can do; paired with env_inspect) │
  └───────────────┬────────────────────────────────────────────────────┘
                  │ read by
  ┌───────────────▼────────────────────────────────────────────────────┐
  │  1 · NapMem pyramid      →  HOW you navigate that substrate         │
  │       (agent drills summary→note→evidence across provenance tiers)  │
  └───────────────┬────────────────────────────────────────────────────┘
                  │ feeds ideation
  ┌───────────────▼──────────────────────┐   ┌─────────────────────────┐
  │  3 · Narrow-Exploration               │   │  2 · SciResearcher      │
  │  the OBJECTIVE: use memory to BROADEN, │   │  a reasoner/loop over it│
  │  not narrow (coverage KPI, already     │   │  (turn literature ON;   │
  │  built → make proactive; quality-      │   │  optional 8B backend;   │
  │  diversity, open-ended mode)           │   │  patterns not framework)│
  └────────────────────────────────────────┘   └─────────────────────────┘
```

- **0 → 1:** the KB is one more tier NapMem navigates; a bigger, better-organized memory is what makes
  navigation (vs flat top-k) pay off.
- **1 → 3:** navigating a provenance pyramid surfaces *far-from-seed precedents* the Researcher misses under
  flat top-k — mechanically counteracting concentration.
- **2 → 0/3:** turning on literature in deep research (2B) *fills* the KB with grounded cited claims **and**
  supplies the far-from-seed material a broadened Researcher (3) needs — the two changes reinforce.
- **3 governs the budget:** the coverage KPI tells the Strategist *when* to spend navigation/deep-research
  budget on distant regions vs exploit the leader.

---

## 5. Consolidated recommendation

Priority-ordered, each mapped to an existing seam; none replaces the engine:

| # | Item | Source | Seam | Effort | Mode | Why now |
|---|---|---|---|---|---|---|
| 1 | **Turn deep research literature-expanding** (default `literature_search`/`web_search` on + wider tool budget) | SciResearcher (2B) | `deep_research.py:214`, `config.py:691,695` | S | opt-in default | Flip two flags; plumbing to act on directions already exists; fills the KB + broadens ideation |
| 2 | **Make the coverage KPI proactive** (dashboard + collapse trigger; add distance-from-seed) | Narrow-Exploration | `coverage.py:50`, `strategist.py:145`, `novelty.py` | S–M | KPI all / action open-ended | The metric is *already computed*; the gap is acting on it before collapse |
| 3 | **Frameworks/Libs KB** (`knowledge/{frameworks,libs}/` + note frontmatter) | общая БЗ | `knowledge_tools.py:163-265` (borrow Skills frontmatter) | S–M | all | Substrate for 1/4/5; small build (frontmatter parse), not zero-code |
| 4 | **NapMem pyramid tools** (activate dormant `CaseLibrary`; provenance drill-down; no RL) | NapMem | `memory.py:514-609`, `memora.py:241-269`, kb tools | M | large-corpus / open-ended | ADR-10 retrieval upgrade; attacks context-rot; a store is already 80% built |
| 5 | **Quality-diversity selection re-activation** (breadth quota / niche-expansion) | Narrow-Exploration | `policy.py:172-288`, `archive.py:20`, `foresight.py:213` | M | open-ended only (gated) | Re-justifies ADR-6-parked machinery for the mode where it matters |
| 6 | **SciResearcher-8B as a deep-research backend** (if released) | SciResearcher (A) | `roles.*.model` (ADR-7) | S | opt-in | Config-only; benchmark before trusting (domain mismatch) |
| 7 | **Self-distillation from `events.jsonl`** (fine-tune a local role) | SciResearcher (C) | *external training project* | L | research direction | We own the trajectory+verified-reward data; but training breaks backend-agnosticism → external only |

### One-line strategy read

> **Ship the two cheap high-value flips first — literature-on in deep research (1) and making the
> already-computed coverage KPI proactive (2); build the curated KB (3) as a small frontmatter addition;
> upgrade retrieval to a NapMem pyramid by activating the dormant `CaseLibrary` (4); re-activate
> quality-diversity behind the open-ended-mode gate (5) with Narrow-Exploration as the justification and the
> coverage KPI as the trigger; treat SciResearcher-8B as an optional backend (6) and shelve self-distillation
> (7) as a research direction.** Nothing here is "simply better" than LoopLab; items 1, 2, 4, 5 are upgrades
> to machinery already in the tree, item 3 is a small build on the existing store, item 6 is config. Only 7
> is a genuinely new capability class (training our own model) — the one thing that breaks
> backend-agnosticism, so it stays opt-in and external.

---

## Sources

- NapMem: *From Passive Retrieval to Active Memory Navigation: Learning to Use Memory as a Structured Action Space* — [arXiv:2607.05794](https://arxiv.org/abs/2607.05794) (abstract/PDF proxy-blocked; claims from search-index snippets).
- SciResearcher: *Scaling Deep Research Agents for Frontier Scientific Reasoning* — [arXiv:2605.01489](https://arxiv.org/abs/2605.01489); secondary review: [themoonlight.io](https://www.themoonlight.io/en/review/sciresearcher-scaling-deep-research-agents-for-frontier-scientific-reasoning).
- Narrow-Exploration: *AI Research Agents Narrow Scientific Exploration* — [arXiv:2605.27905](https://arxiv.org/abs/2605.27905); [HuggingFace paper page](https://huggingface.co/papers/2605.27905).
- Related: *Heuresis: Search Strategies for Autonomous AI Research Agents Across Quality, Diversity and Novelty* — [arXiv:2606.25198](https://arxiv.org/abs/2606.25198).
- Internal: [ADR-6](03-decisions.md) (2026 SOTA re-prioritization), [ADR-9](03-decisions.md) (MCP + Skills), [ADR-10](03-decisions.md) (unified knowledge/memory), [ADR-16](05-build-decisions.md) (RAG/vector store), [guide/memory.md](guide/memory.md), [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md).
</content>
