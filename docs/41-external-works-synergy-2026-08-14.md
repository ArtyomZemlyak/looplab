# LoopLab — External Works Synergy: AREX · Skill-SP · Frontis-MA1/OpenMLE · EvoLib · PACEvolve (2026-08-14)

**Status: analysis; nothing here is shipped.** This is a dated external-works analysis in the
[doc 13](13-external-works-analysis-2026-07.md) / [doc 26](26-ouroboros-airi-analysis-2026-08-02.md)
line: five works published or released around July–August 2026, each checked against its primary
source, each mapped onto verified LoopLab module paths (checked against current `master` on
2026-08-14). It does not flip a default, does not prove integration, and never outranks source/tests.
Companion authorities: [doc 28](28-deep-research-sota-roadmap-2026-08-10.md) (Deep Research proposal
ledger, DR-xx ids), [doc 27](27-agent-system-mega-review-2026-08-09.md) (agent-system review + eval
ladder), [doc 36](36-agent-driven-decisions-2026-08-13.md) (the NEXT-vs-RECORD principle),
[doc 29](29-operator-backlog-2026-08-11.md) (operator backlog + measured memory-store numbers).

**The five, in one line each:**

| Work | Source | One-line claim |
|---|---|---|
| AREX (BAAI) | <https://arxiv.org/abs/2607.21461> | Recursive research agent: an outer audit loop verifies constraint-by-constraint and *directs* the next research round; a trained context-update tool compacts history into "verified evidence + unresolved constraints". |
| Skill-SP (Qwen/Alibaba) | <https://arxiv.org/abs/2607.22529> | Self-play through a skill library: skills guide task generation AND verify solutions; a Controller evolves the library on execution feedback. +42.9 pts tool calling on Qwen3-4B vs plain self-play. |
| Frontis-MA1 / OpenMLE (Frontis AI) | <https://arxiv.org/abs/2607.28568> | 35B meta-evolution MLE agent: 5,758-task verifiable gym, SFT+RL-trained Draft/Improve/Debug/Crossover operators, evolutionary search with experience memory. MLE-Bench Lite Medal Average 39.39%→60.61% (71.21% Evo-Max) in 12 h on one RTX 4090. |
| EvoLib (Microsoft Research) | <https://www.microsoft.com/en-us/research/blog/evolib-turning-experience-into-evolving-knowledge/> (blog + GitHub release) | Memory as evolving knowledge, no weight training, model-agnostic: skill-from-success / insight-from-failure, consolidation of similar knowledge into more general, utility weighting updated by downstream usefulness (= principled forgetting). Beats retrieval-based memory. |
| PACEvolve (DeepMind + UCSD et al.; sequel PACEvolve++) | <https://arxiv.org/abs/2601.10657> | Names three failure modes of LLM evolutionary search — Context Pollution, Mode Collapse, Weak Collaboration — and fixes each with hierarchical context pruning, momentum-based backtracking, and self-adaptive sampling unifying backtracking + crossover. Validated on Symbolic Regression and KernelBench. |

---

## 1. AREX — verifier-directed research, already specified here as DR-04/DR-09

**What it is.** An inner research phase gathers evidence and drafts an intermediate answer; an outer
loop audits it *constraint by constraint*, exploiting what the authors call discovery–verification
asymmetry: finding a solution under all constraints at once is expensive, checking each constraint in
isolation is cheap. Verification output is not a final filter — every unverified claim seeds targeted
follow-up research in the next round. A trained context-update tool compresses the transcript into a
compact state of verified evidence plus unresolved constraints. 4B dense and 122B-A10B MoE variants;
SOTA-competitive on BrowseComp / WideSearch / DeepSearchQA / HLE.

**What it validates in LoopLab.** This is external, at-scale validation of exactly the two P0/P1
items doc 28 already specifies: **DR-04** (verifier-directed retrieve/revise/replan — "verification
must control the next step", not annotate a memo) and **DR-09** (typed compaction — plan, decisions,
gaps, evidence ids and remaining budget survive compaction losslessly, instead of a generic history
summary). AREX's "unresolved constraints" carrier is DR-01's `ProgressLedger`. LoopLab's event log
makes the whole pattern *cheaper* than AREX's: provenance of every verified item is free (append-only
`events.jsonl` + pure `fold`), where AREX has to train a tool to preserve it through compaction.

**What to borrow, and where.**

- The *loop shape*: verification verdicts become the work queue of the next round. Landing spot is
  the doc 28 DR-01+DR-02 slice (durable `ResearchEpisode`/`ProgressLedger` + evidence identity), then
  DR-04 wiring in `agents/deep_research.py` + `agents/tool_loop.py` — the tool loop already exists,
  what is missing is the durable ledger the verdicts feed.
- Constraint-by-constraint decomposition maps onto DR-03's four independent verification questions
  (integrity / coverage / support / freshness) rather than one holistic judge call.
- Typed compaction (DR-09): the compaction *schema* is deterministic and replayable; only the
  summarization of free text inside a typed slot is a model call. That split is the doc 36 line.

**What NOT to borrow.** The *trained* context-update tool. LoopLab is model-agnostic by charter and
has no weight-training loop; a typed, schema-owned compaction over the event log gets the same
lossless-state property without training. Also do not borrow "verifier as the same model reviewing
itself" — LoopLab's `trust/memo_verify.py` verdict path must stay a separate, deterministic-shelled
gate (and see §7: its `finalize_verified_evidence` lifecycle-only check is a known open gap).

## 2. Skill-SP — the skill *lifecycle* is the transferable half

**What it is.** Self-play where a skill library is the pivot: a skill guides the Proposer's task
generation and gives the Solver's verification signal; a Controller evolves the library from
execution feedback (refine / delete / induce skills). This resolves the classic self-play dilemma —
narrow environments with verifiers vs unverifiable self-generated chaos — and yields +42.9 points on
tool-calling benchmarks for Qwen3-4B over ordinary self-play.

**What it pressures in LoopLab.** LoopLab has auto-distilled skills (M4:
`engine/lessons_distill.py` writes `skills/*.md` from supported Card work items;
`tools/skills.py` serves them with progressive disclosure and a small `provenance`/`status`
frontmatter lifecycle gating production visibility). What it does **not** have — recorded since
[doc 17](17-project-review-and-directions-2026-07-11.md) §15 (EvoDS) — is a *live* lifecycle:
nothing ever re-validates a skill against later execution feedback, refines it, or retires it. A
skill distilled once is trusted forever.

**What to borrow, and where.**

- **Validate/refine/retire on execution feedback.** The feedback already exists durably: every run's
  `events.jsonl` records which nodes ran with which skills in context, and their terminals. A
  finalize-time pass (beside `engine/lessons_distill.py`, consuming folded `RunState`) can update a
  skill's `status` frontmatter — the field already exists in `tools/skills.py` — from
  candidate → validated → retired, with the evidence run/node ids stamped in.
- The doc 36 split is mandatory: **the LLM proposes a skill's semantics; deterministic code owns its
  identity, provenance and invalidation.** A model may draft a refined body; only code may flip
  `status`, and only from recorded outcomes.

**What NOT to borrow.** The whole self-play RL contour — Proposer/Solver task generation to train
*weights* is outside LoopLab's charter: self-evolution here is reviewed and charter-bound
([doc 26](26-ouroboros-airi-analysis-2026-08-02.md) §4.2 #12), and the improvement target is the
external candidate solution, not the harness or the model. Also do not let skills become a
verification *authority*: in Skill-SP a skill verifies solutions; in LoopLab verification belongs to
the trust layer, and a skill is retrieval context only.

## 3. Frontis-MA1 / OpenMLE — benchmark pressure, and a free eval corpus

**What it is.** A 35B meta-evolution agent for ML engineering. Three artifacts: **OpenMLE-Gym**
(5,758 verifiable, execution-backed MLE tasks), **OpenMLE-RL** (Draft/Improve/Debug/Crossover
operators trained with SFT+RL on execution feedback), **OpenMLE-Evo** (evolutionary search with
experience memory). On MLE-Bench Lite (12 h, one RTX 4090 12GB): Medal Average 39.39%→60.61%, and
71.21% with Evo-Max — above GPT-5.5+Codex. Weights/code/data are promised open.

**What it pressures in LoopLab.** Two things, directly:

1. **LoopLab has zero real MLE-bench runs with a private held-out grader.** `docs/MLEBENCH.md` is a
   runbook and the `mlebench_real` task kind exists in `adapters/`, but no published number. A 35B
   open-weight agent posting 60%+ Medal Average makes "we have the better architecture" unfalsifiable
   until a run exists.
2. **OpenMLE-Gym is the eval corpus doc 27's agent eval ladder (§4) and doc 28's DR-11 both name as
   missing** — consume it, do not build a bespoke one from scratch.

**What to borrow, and where.**

- Run the benchmark. The path exists end-to-end: `adapters/` MLE-bench task kind → sandbox tiers in
  `runtime/sandbox.py` → trust gates. What a real run uniquely exercises is the fresh provenance
  hardening: `runtime/read_fence.py`, `runtime/metric_subject.py` (+ `runtime/read_allowlist.py` /
  `runtime/landlock.py`), and `engine/metric_salvage.py`.
- Ingest OpenMLE-Gym tasks as a task source behind the existing TaskAdapter contract
  (`adapters/tasks.py`) once released — 5,758 execution-verified tasks is the regression corpus the
  doc 27 eval ladder lacks.

**What NOT to borrow.** The RL-trained operators. Their differentiator is trained operators; ours is
**trust/replay/provenance, which they do not have** — no leakage/reward-hack/CV gates, no replayable
log, no metric-subject binding. Chasing operator RL concedes our differentiator to compete on
theirs. The right response is to run their benchmark *through* our trust layer and report
hack-adjusted numbers beside the raw Medal Average — a number nobody else on that leaderboard can
produce.

## 4. EvoLib — the direct treatment for LoopLab's measured memory rot

**What it is.** Training-free, model-agnostic memory-as-evolving-knowledge: extract a reusable skill
from each success and a reflective insight from each failure; **consolidate** new knowledge with
similar existing knowledge into something more general; **dynamically weight** each item by immediate
usefulness AND by its contribution to generating useful knowledge on later tasks — which gives
principled forgetting. Consistently beats retrieval-based memory and converts test-time compute to
quality more efficiently.

**What it pressures in LoopLab.** The cross-run store is measured rot
([doc 29](29-operator-backlog-2026-08-11.md), tail): 161 lessons of which 7 from a real task, 163
notes of which 71 distinct (one repeated 23×), 10 cases of which 9 are test fixtures. This is
[doc 11](11-agent-systems-research.md) D2 ("memory hygiene before memory growth", G3 misevolution)
made concrete — and EvoLib is its operationalization.

**What exists already** (verified 2026-08-14): `engine/lesson_hygiene.py::consolidate_lessons` does
exact-dedup + contradiction resolution (newest verdict wins) plus an agentic paraphrase-merge pass;
`search/hybrid_merge.py::consolidate` is the shared RRF retrieval + agent-decided merge both lesson
and hypothesis-board consolidation use. The success/failure split also exists: lessons carry
verdicts, and `engine/metric_salvage.py` already withholds unmeasured claims from memory.

**What is missing, and where it lands.**

- **Generalization, not just dedup.** `consolidate_lessons` merges within a `(task_id, role)` bucket
  only — "similar → more general" *across* tasks does not exist. A cross-task pass belongs beside
  `engine/lesson_hygiene.py`, reusing `hybrid_merge.consolidate` (LLM proposes the generalized
  statement; deterministic code keeps `evidence_refs` to every source row — doc 36 again).
- **Utility scoring updated by use.** Nothing records whether an injected prior helped. The write
  side is a per-lesson usage/outcome counter updated at run finalization (a sibling of
  `evidence_count`, which `retrieve_lessons_harmonic` already ranks by); the measurement side is the
  **prior-injection hit-rate audit** — doc 26 §4.2 #9, offline over memory dirs + event logs.
- **Forgetting as a ranked outcome, not deletion.** Down-weighted items fall out of the bounded
  retrieval window (`core/memory_window.py`) naturally; destructive pruning stays an operator
  (governance) action, per the `governance_cmds` boundary.

**What NOT to borrow.** Fully autonomous memory rewriting. EvoLib lets the loop rewrite its own
knowledge unsupervised; LoopLab's G3 misevolution analysis says exactly that is the risk with
`reflection_priors` ON. Consolidation output must remain attributable (evidence refs), replayable,
and quarantine-able — the `lesson_guard`/`unreliable_metric_ids` joins must survive any merge.

## 5. PACEvolve — three search failure modes, all expressible as policies over folded state

**What it is.** A diagnosis-first paper: LLM evolutionary search fails by **Context Pollution**
(failure history contaminates generation — fixed by Hierarchical Context Management with pruning),
**Mode Collapse** (stuck in a local optimum — fixed by Momentum-Based Backtracking on a real-time
momentum signal), and **Weak Collaboration** (rigid crossover ignores parallel trajectories — fixed
by self-adaptive sampling unifying backtracking and crossover). Validated on Symbolic Regression and
KernelBench; sequel PACEvolve++; the adjacent Evo-Memory benchmark covers test-time learning with
self-evolving memory (relevant to §4's measurement, not to search).

**Mapping, checked against current code.**

- *Context pollution* ≈ [doc 10](10-autoresearch-improvement-research.md) M5 / doc 11 G8. Partially
  overtaken: M5 shipped — `events/digest.py::auto_char_cap` scales the digest with the run and the
  sampler keeps a representative best+worst spread. What remains open is *hierarchical* context —
  lineage/sibling scoping and explicit pruning of exhausted failure branches from the operator
  context (G8's half).
- *Mode collapse* ≈ doc 11 D3/G4, the long-standing stagnation-adaptive policy switch. Still absent:
  no stagnation signal exists anywhere in `search/` or `engine/strategy.py` (grep-verified). The
  Strategist (`engine/strategy.py` + `agents/strategist.py`) is the designated host; momentum is
  computable from folded `RunState` (best-metric trajectory), and backtracking is "re-anchor
  proposals on an earlier node", which the multi-parent DAG already represents.
- *Weak collaboration* ≈ doc 10 T8, and **T8 is still true**: `search/operators.py::merge_idea`
  (lines 21–44) mean-merges numerically coercible params and skips everything else — for a repo task
  a "merge" recombines nothing. Real recombination is an agent-decided code merge: the machinery is
  `search/hybrid_merge.py`'s agent-decided merge pattern applied to parent *code*, fired by
  `search/policy.py` (which owns when operators fire), with `merge_idea` kept as the fallback for
  numeric-param tasks.

**What NOT to borrow.** Nothing here requires new event types or background writers — all three
mechanisms are decision policies over `fold(store.read_all())` and must stay that way (invariants
1/3/4). Momentum must be a *derived* value recomputed per fold, never cached engine state.

## 6. Synergy: how the pieces compose

The five works are one contour seen from five angles, and LoopLab already owns the substrate each of
them had to build ad hoc — the append-only event log with pure replay:

1. **AREX's verifier-directed cycle** (= DR-04/DR-09 over DR-01/DR-02 ledgers) makes verification
   the *driver* of research, with unresolved constraints as durable work items.
2. **EvoLib-shaped memory** (consolidation + utility + forgetting over
   `engine/lesson_hygiene.py` / `search/hybrid_merge.py`) makes what the loop *learns* compound
   instead of rot — and the prior-injection hit-rate audit (doc 26 §4.2 #9) measures whether it does.
3. **Skill-SP's lifecycle** applies the same discipline to procedural memory: skills get validated,
   refined and retired on recorded execution outcomes instead of being trusted forever.
4. **PACEvolve's three mechanisms** keep the search itself healthy: pruned hierarchical context in,
   stagnation-triggered backtracking when the momentum dies, real recombination across parallel
   trajectories instead of mean-of-params.
5. **The trust layer is the differentiator that makes the composition publishable**: every claim the
   memory keeps, every skill promoted, every verified research item, and every benchmark number runs
   through leakage/reward-hack/CV/confirm gates and metric-subject binding — which none of the five
   works has. Frontis-MA1 supplies the arena (OpenMLE-Gym / MLE-Bench Lite) where that difference
   becomes a reportable number: raw Medal Average *and* hack-adjusted score.

The dependency order matters: memory hygiene (2) before memory growth; skill lifecycle (3) is memory
hygiene applied to skills; verifier-directed research (1) produces the verified claims memory should
keep; search mechanisms (4) consume the healthier context; the benchmark (5) proves the stack.

## 7. What must NOT change

- **Engine invariants 1–7** (CLAUDE.md): the engine is the sole writer of domain events; one
  terminal per node; every side effect gated on a domain event; state only via
  `fold(store.read_all())`; `fold` deterministic and order-tolerant; `run_started` settings win on
  resume; event types registered in `events/types.py`. Every proposal above is a policy over folded
  state or a finalize-time writer precisely so none of them touch these.
- **The doc 36 principle**: an agent may decide what happens NEXT (a merge, a refinement, a
  backtrack, a skill body); deterministic code over authenticated evidence owns what goes into the
  RECORD (skill status, lesson evidence refs, utility counters, verification verdict identities).
- **Charter-bound self-evolution** (doc 26 §3.1/§4.2 #12): no weight training, no unsupervised
  harness self-modification. Skill-SP's RL contour and OpenMLE-RL's trained operators stay out.
- **The trust layer's independence.** Known open gap, do not widen it:
  `trust/memo_verify.py::finalize_verified_evidence` checks a cited node's *lifecycle* but not its
  feasibility or trust flags, so a D8 claim can still ratify `supported` on a salvaged or
  reward-hacked node (grep-verified still true on 2026-08-14). New memory writers must join
  `engine/memory.py::unreliable_metric_ids`, not bypass it.

## 8. Recommended order

1. **Landlock validation + `metric_subject`.** Complete one real train+score under
   `landlock="enforce"` with `looplab landlock-check` clean, and exercise `metric_subject` binding
   (`runtime/landlock.py`, `runtime/metric_subject.py`; docs [35](35-metric-provenance-core-options-2026-08-13.md)
   and [38](38-fence-coverage-audit-2026-08-13.md)). Everything downstream reports numbers; the
   numbers must be about the bytes the node produced first.
2. **Close the `memo_verify` gap** (§7): feasibility + trust-flag checks in
   `finalize_verified_evidence`, so `research_claims.jsonl` cannot ratify a claim on a node the
   selection layer already distrusts. Small, deterministic, and a precondition for trusting memory.
3. **Memory consolidation + hit-rate audit** (§4): cross-task generalization pass and utility
   counters over `engine/lesson_hygiene.py`/`search/hybrid_merge.py`, measured by the doc 26 §4.2 #9
   prior-injection hit-rate audit — the audit ships in the same change, or the consolidation is
   unfalsifiable.
4. **The DR-01/DR-02/DR-03 slice** from doc 28, in its own stated order (DR-01+DR-02 as one design
   slice, DR-03 deterministic gates next) — the substrate DR-04's AREX-shaped loop then lands on.
5. **A real MLE-bench run, then OpenMLE-Gym ingestion** (§3): one MLE-Bench Lite run with the
   private held-out grader through the full trust layer, reporting raw and hack-adjusted scores;
   then OpenMLE-Gym as a TaskAdapter-backed eval corpus for the doc 27 eval ladder / DR-11.

Steps 1–2 are hardening already-shipped surfaces; 3–4 are the two compounding loops (memory,
research); 5 is the external proof. PACEvolve's search mechanisms (§5) and the skill lifecycle (§2)
slot in behind 3 as policy work with no new invariant surface.
