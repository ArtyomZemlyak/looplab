# LoopLab — Deep Improvement Research: Auto-Research Engine (2026-07-02)

**Method:** four parallel investigation passes over (1) the core engine (`looplab/`, ~23k lines),
(2) planning & memory mechanisms, (3) the React UI + server, and (4) a fresh web sweep of
2025–2026 SOTA autonomous-ML-research systems — cross-checked against
[ROADMAP.md](ROADMAP.md) / [BACKLOG.md](BACKLOG.md) to avoid re-proposing what is already planned
or shipped. Companion to [07-architecture-review.md](07-architecture-review.md).

**TL;DR.** The engineering backbone (event-sourced loop, replay-resume, pluggable
roles/policies, Strategist, rich live UI) is genuinely strong and most of the 2026-06 roadmap
*shipped*. The three biggest remaining gaps are not missing features — they are:
**(1) the default run is dumb** (nearly every intelligence feature is opt-in and OFF);
**(2) planning and memory are shallow** (no hypothesis ledger, no plan artifact, exact-task-keyed
one-line cross-run memory, fake hash-embeddings); **(3) the UI can steer but cannot show intent**
(no experiment queue, no hypothesis board, no any-vs-any diff). Details and a prioritized plan
below.

---

## 1. Where the system actually stands (verified in code)

**Strong and real (do not re-propose):** event log as single source of truth with pure
order-tolerant `fold` (`replay.py`), crash-resume, three+ search policies (greedy / evolutionary /
MCTS / ASHA / BOHB in `policy.py`), Strategist meta-controller with `off|rule|llm|agent` backends
(`strategist.py`), Deep-Research stage (`deep_research.py`), cross-run case library + reflection
notes (`memory.py`), knowledge/skills markdown with agentic retrieval (`knowledge_tools.py`,
`skills.py`), code-block ablation (`orchestrator.py:2304+`), per-role/per-stage model routing
(`tasks.py`), inter-token LLM watchdog (`llm.py`), and a chat-first UI (AssistantBar/Dock,
boss-mode, HITL gates, time-travel, fork/inject).

**The headline finding — the stock run uses almost none of it.** Defaults in `config.py`:

| Setting | Default | Effect of default |
|---|---|---|
| `policy` | `greedy` | always exploits the single global best (`policy.py:139`) |
| `max_nodes` | 8 | very shallow tree |
| `max_parallel` | 1 | fully sequential loop |
| `novelty_gate` | `False` | duplicate ideas are evaluated repeatedly |
| `confirm_top_k` / `confirm_seeds` | 0 / 0 | multi-seed confirmation never runs; a seed-lucky node wins |
| `reward_hack_detect` | `False` | hack/leakage detectors don't even run |
| `ablate_every` | 0 | ablation-targeted refinement never fires |
| `deep_research_every` | 0 | Deep-Research never fires on cadence |

Trust detectors that *do* run are **audit-only** — they never change selection
(`orchestrator.py:2146-2177`). So the out-of-the-box experience — the thing being demoed and
benchmarked — is: greedy, shallow, sequential, unconfirmed, unguarded. Every gap below is
amplified by this.

> **Update (this session).** The table above describes the *pre-change* defaults. Since then:
> the `thorough` profile (T1) turns the trust/confirm/ablation machinery on in one word; the P1
> hypothesis ledger (`track_hypotheses`) and the P4 cross-run memory + lessons (`reflection_priors`)
> now **default ON** (audit-only, and cross-run memory is a no-op until `memory_dir` is set). Every
> one of these remains an individually-toggleable setting, surfaced in the CLI/YAML **and** the UI
> Settings form.

---

## 2. Technical improvements (engine)

### T1 · A "research-grade" preset — fix the defaults problem *(quick win, highest leverage-per-effort)* — ✅ SHIPPED
One config preset (`profile: thorough` vs `default`/`fast`) that turns on what already exists:
novelty gate, confirm top-k (k=3, 3 seeds), reward-hack + leakage gates *in enforcing mode*,
ablation cadence, complexity cue, budget cue, failure reflection, reflection priors. Rationale: the
marginal cost is config only; the machinery is built and tested. This single change moves the median
run more than any new feature.
- **Implemented** (`config.py`): `PROFILES` map + a `profile` field + a `model_validator(mode="before")`
  that expands the profile *config-first* — it fills only fields the user did not set, so any explicit
  file/CLI/`LOOPLAB_*` knob wins. Surfaced in the CLI (`-s profile=thorough`), YAML, the UI Settings
  form (`ui/src/settingsSchema.js`), and the guide. Deliberately touches only quality/trust knobs,
  not spend (`max_nodes`/`max_parallel` stay the user's). Regression-tested in
  `tests/test_profile_trust_gate.py`.

### T2 · Advisory → enforcing trust ladder — ✅ SHIPPED
Detectors exist (reward-hack, leakage, critic, drift) but a flagged node could still be promoted to
champion. New knob `trust_gate: audit|gate|block`:
- `audit` (default) = surface only, never change selection (legacy behavior);
- `gate` = a flagged node is excluded from best-selection but stays in the tree (the search may still
  repair/improve it into a clean version);
- `block` = additionally mark it infeasible so the policy won't breed from it either.
- **Implemented** replay-safe: the mode is recorded in the `run_started` event (`orchestrator.py`) so
  the pure `fold` (`replay.py`) applies the same gate on replay/resume — old logs (no field) fold to
  `audit`, byte-identical. Gates **only** high-precision cheating/leakage signals; the heuristic
  `critic:` signal stays advisory in every mode. Order-independent (computed from folded
  `reward_hacks`). `thorough` sets `trust_gate=gate`. Regression-tested in `tests/test_profile_trust_gate.py`.

### T3 · Un-park B6: held-out split + generalization-gap guard
Still the #1 verified unsolved failure mode (val–test gap 15–16.6%, arXiv:2507.02554 §5.3), and
the engine currently selects `best` purely on the search metric. Minimum viable version: the eval
contract grows an optional `holdout` reader the search never sees; champion selection among the
val-top-k happens on holdout; per-node `generalization_gap` folds into the Trust panel. This gates
any publishable MLE-bench number.

### T4 · Real embeddings behind the existing `VectorStore` seam — ✅ SHIPPED
`hash_embed` (`knowledge_tools.py`, `memory.py`) is a lexical hashing trick — KB search and case
retrieval are built on it, so both quietly underperform. Wire an optional local embedding model (the
box already runs Ollama — `nomic-embed-text` / `bge-m3` via the same OpenAI-compatible endpoint; keep
hash_embed as the zero-dep fallback).
- **Implemented** (`vectorstore.py`): `LLMEmbedder` (stdlib urllib → `/embeddings`, same proxy/CA as
  the chat client) + `make_embedder(settings)`; config `embed_model`/`embed_base_url` (blank =
  hash_embed, byte-identical). **Robust by construction**: the embedder commits to one vector
  dimension for its lifetime and degrades to a same-dim `hash_embed` fallback on any endpoint failure,
  so `_cosine` never sees a dim mismatch and an offline/flaky endpoint never crashes a run — it only
  loses semantic quality. Threaded through `KnowledgeTools` (one embedder builds *and* queries the
  index). Surfaced in Settings UI + guide. Tested in `tests/test_embedder.py`.

### T5 · Semantic novelty/dedup for ideas (not just numeric params)
The novelty gate is L2 over numeric params and *jitters* duplicates instead of rejecting them —
useless for repo/code/free-form ideas. With T4 in place, dedup on `embedding(idea.approach +
rationale)` against accepted+failed nodes; a near-duplicate of a *failed* idea should be rejected
with the failure surfaced to the Researcher ("you already tried X, it scored Y because Z").

### T6 · Fold caching / incremental state — ✅ SHIPPED (read-cache form)
`fold(store.read_all())` re-parsed the whole log many times per iteration and every 0.3 s in the
abort watcher — O(events²) IO+orjson+`Event()` per run, the main scaling ceiling.
- **Implemented** as an **incremental read cache in `EventStore`** (lowest-risk form — `fold` itself
  is untouched, so its 838-test correctness contract stands): `read_all()` keeps already-parsed
  Events and reads only the bytes appended since the last call (byte-offset cursor ending on a newline
  boundary), rebuilding on a shrink/heal-truncate. It returns byte-for-byte what a fresh `iter_jsonl`
  scan would (same torn/corrupt-tail rules), is thread-safe (a lock guards the top-up, for the
  concurrent watchers under `max_parallel>1`), and the abort watcher now scans the cache instead of
  re-reading the file. **Measured ~200× on the loop's read pattern** (3000-event log, 300 reads).
  Verified with a parallel toy run + an 8-thread read-stress; tested in `tests/test_eventstore_cache.py`.
- *Left for later:* an incremental *fold* (continue a cached `RunState` over only new events) — the
  read cache already removes the dominant IO+parse cost, so the remaining O(events) pure-Python
  reduce is cheap; splitting `fold` into reducer+finalize is a larger, higher-risk change deferred
  until profiling shows it matters.

### T7 · LLM response cache + prompt-prefix discipline
Identical/near-identical calls re-hit the model every time. A content-addressed response cache
(`hash(model, messages) → completion`, stored under the run dir, replay-safe because Ideas are
already recorded in events) cuts cost on resume/retry/panel flows. Keep stable system-prompt
prefixes so server-side prompt caching (vLLM/SGLang/hosted APIs) actually hits.

### T8 · Default merge should be code recombination, not mean-of-params
`merge_idea` still averages numeric params (`operators.py:16`); the verified-strongest operator
(agent-proposed iterative ensembling — MLE-STAR: 37.9%→43.9%; removing merge: −9 pp in KompeteAI)
exists behind `merge_mode="ensemble"` but is not the default for code-bearing tasks. Flip the
default per task-kind (code/repo/dataset → ensemble; numeric-only → mean is fine).

### T9 · Sandbox: minimum-viable resource limits in the default tier
`trusted_local` has timeout + tree-kill only — no memory cap (one node can OOM the whole engine)
and network on. Cheap wins without Docker: `resource.setrlimit` (RLIMIT_AS/RSS) + optional
`unshare -n` network-off on Linux; keep Docker/gVisor tiers for untrusted. Also finish the two
parked P0s: out-of-process mlebench grader (labels currently importable by the candidate,
`mlebench.py:102`) and stdout redaction before events (`orchestrator.py:808`).

### T10 · Deeper debug + failure triage
`debug_depth=1` abandons lineages after one repair; the anti-stuck check compares exact error
signatures and misses semantically-identical errors. Raise default depth to 2–3 with the existing
A0e ReAct-style bounded act/observe loop, and normalize signatures (strip addresses/line numbers)
before the stuck check.

---

## 3. Planning improvements

The current "plan" is a derived label (`seed|explore|exploit|confirm` computed from node counts,
`strategist.py:101-108`) plus a reactive rule table. Nothing represents *what the run intends to
find out*. Three additions, all replay-safe events:

### P1 · Hypothesis ledger (first-class hypotheses) — ✅ SHIPPED
A `Hypothesis` `{id, statement, source, status (open|testing|supported|tested|abandoned), evidence:
[node_ids], best_delta}`. This is the single biggest *planning* upgrade: it converts the loop from
"propose next mutation" to "run experiments that resolve open questions", and gives the UI a real
board (U2). External precedent: Kosmos (arXiv:2511.02824) keeps a queryable "structured world model"
of entities/results/*open questions* — the ledger is the LoopLab-native, event-sourced version.
- **Implemented DERIVED-in-fold** (lowest-risk, fully replay-safe, audit-only — never touches
  best-selection): `Idea` gains an optional `hypothesis` statement (the Researcher states what each
  experiment tests; prompt updated); `replay._derive_hypotheses` builds the ledger from every node
  carrying one (id = slug of the statement, so the same claim from several ideas links to one entry)
  plus explicit `hypothesis_added` events. The **verdict is computed from outcomes** — supported when
  an experiment improved over its parent / became best, tested when evaluated without improvement,
  testing while evidence runs, open with no evidence; a human `hypothesis_updated status=abandoned`
  overrides. **Deep-research directions now register as open hypotheses** (were fire-and-forget) and a
  human can add/abandon from the board via control events. Tested in `tests/test_hypotheses.py` +
  a server round-trip in `tests/test_server.py`.

### P2 · Plan-as-artifact with budget allocation + re-planning
An explicit `plan` event: ranked experiment backlog (from Genesis/Deep-Research/Strategist) with
per-phase budget split ("spend ≤30% of eval seconds on exploration, reserve 20% for confirm/
ensemble endgame"). The Strategist already has the state inputs; today it can only swap machinery,
not allocate budget across intents. Endgame discipline matters: top MLE-bench systems reserve an
explicit final-ensemble + confirmation window instead of exploring until the budget dies.
External precedent: AI-Scientist-v2 runs its tree search through four explicit stages with
stopping criteria (preliminary investigation → tuning → agenda execution → ablations), each with
its own budget — the staged version of the same idea.

### P3 · Run-level ablation attribution
A0a ablation is per-node; nothing aggregates "which pipeline component moved the metric across the
whole run". Fold per-component deltas into a run-level attribution table (data-prep / features /
model / loss / ensemble) and let the Strategist bias the next batch toward the highest-yield
component — MLE-STAR's outer loop, currently missing. Also feeds the report ("what actually
mattered") and F1 importance view.

### P4 · Bandit over operators
Operator cadence is fixed (`merge_every=3`, etc.). Track per-operator yield (Δmetric per
eval-second) in the fold — the data is already in events — and let a tiny UCB/Thompson layer pick
the next operator mix. This is the cheap, principled version of "adaptive search" that doesn't
need MCTS; the Strategist's rule table becomes priors rather than hard-coded thresholds.

---

## 4. Memory improvements

Memory exists at three tiers but each is shallow:

### M1 · Operator-scoped memory (A0c — designed, still unbuilt)
The digest is one flat 1200-char global summary for every operator. Port the aira-dojo `MEM_OPS`
shape: **draft/improve see sibling summaries** (push diversity — "your siblings already tried
A/B/C"), **debug sees the ancestral repair chain** (avoid undo↔redo oscillation). Verified lever;
small change to `_state_brief`/digest assembly.

### M2 · Task fingerprinting for cross-run transfer — ✅ SHIPPED
`meta_notes.jsonl` priors were keyed by exact `task_id` — a similar-but-new task got nothing.
- **Implemented** (`memory.py`): `task_fingerprint(kind, direction, goal, metric, param_names)` →
  a deterministic token set; `fingerprint_similarity` = Jaccard overlap. `_load_reflection_priors`
  now fingerprints the current task and pulls lessons from runs whose fingerprint overlaps
  (sim ≥ 0.34) — turning the case library from "resume my last run" into transfer across *similar*
  tasks. (Deterministic token overlap, not embeddings — zero-dep and enough for the signal; can move
  to T4 embeddings later.) Tested in `tests/test_lessons_fingerprint.py`.

### M3 · Remember failures, not only winners — ✅ SHIPPED
`_write_reflection_note` distilled only the winner. Memory/experience accumulation is the dominant
2026 theme (ML-Master 2.0's Hierarchical Cognitive Caching 29.3%→56.4%; MARS's reflective lessons
are ablation-critical) and remembering what DIDN'T work stops a later run re-treading a dead end.
- **Implemented**: at run end (same `reflection_priors` gate, default off) the engine writes
  structured lessons to `lessons.jsonl` — the winner **plus** every resolved P1 hypothesis
  (`supported`/`tested`/`abandoned` — the ledger yields negative results for free) **plus** the
  dominant failure reason — each `{task_id, fingerprint, statement, outcome, delta, confidence}`.
  `_load_reflection_priors` injects the top-5 fingerprint-matched lessons (positive *and* negative)
  into the proposal prompt. Ties Phase 3 → Phase 4: the hypothesis board is the lesson source.
  Tested in `tests/test_lessons_fingerprint.py`.

### M4 · Auto-distilled skills (procedural memory that grows)
`SkillLibrary` reads static hand-written SKILL.md files. After a run where a technique repeatedly
won (e.g. a CV scheme, a feature recipe), have the Deep-Research/report stage draft a new SKILL.md
into `memory_dir/skills/` (marked `provenance: auto, status: candidate`; promoted after it wins on
a second distinct task fingerprint). This closes the loop from episodic → procedural memory and is
rare among competitor systems.

### M5 · Digest that scales with the run
`char_cap=1200` for an 8-node toy run and a 200-node MLE-bench run alike. Scale the working-set
budget with model context (the `context_budget.py` machinery already exists), and structure it:
per-branch one-liners + open-hypothesis list (P1) + component attribution (P3) instead of a flat
top-5/worst-3.

---

## 5. Interface improvements

The UI is already unusually strong (live DAG, Inspector with diff-vs-parent/trace/cost, 17 panels,
chat-first bar, boss-mode, HITL gates, time-travel, cross-run map). The gaps are about *showing
the system's mind* and *direct manipulation*:

### U1 · Experiment queue / scheduler view *(top gap)* — ✅ SHIPPED
Pending/planned work was invisible — users inferred state from the event feed. Now a primary
`Queue` panel (`ui/src/panels.jsx` `QueuePanel`): pending experiments (op / parents / hypothesis)
each cancelable via `node_abort`, plus queued inject/fork/confirm/ablate requests as read-only
chips. Order is policy-driven (the engine picks next), so it's see + cancel + add, not a fake manual
reorder (no engine event supports reordering the policy's choice).

### U2 · Hypothesis board (needs P1) — ✅ SHIPPED
A kanban: open / testing / supported / tested / abandoned, each card linking to its evidence nodes
and its best Δ-over-parent. This answers the researcher's actual question ("what have we learned?")
which no metric chart answers, and makes the deep-research directions actionable and auditable.
- **Implemented** as a primary `Hypotheses` panel (`ui/src/panels.jsx` `HypothesisBoard`, wired in
  `RunView.jsx`, styled in `styles.css`): five verdict columns, source icons
  monochrome source glyphs (`OpIcon` search/bulb/user/compass — no emoji), click-through to evidence
  nodes, a "+ Add" box and per-card abandon via the `hypothesis_added` / `hypothesis_updated`
  control events. Pure projection of `state.hypotheses`; UI bundle builds clean.

### U3 · Canvas as control surface, not just a view — ✅ SHIPPED
Right-click a node → Explore from here (`fork`) / Ablate (`force_ablate`) / Diff vs champion (opens
Compare seeded champion-vs-node) / Inspect / Kill branch (aborts the node's pending subtree); and
drag a node onto another → merge (manual intersection over the laid-out positions). A "Merge with…"
arm mode (next click, Esc cancels) is the layout-friendly alt to drag. Engine: `_create_injected_node`
grew a multi-parent `parent_ids` path that routes a 2-parent `merge` through the real merge/ensemble
operator, so a canvas merge is a genuine recombination — the only engine change; the rest is
affordances over existing events.

### U4 · Diff any-vs-any + multi-run overlay — ✅ SHIPPED
`ComparePanel` gains a `diff` toggle → line diff of ANY two selected nodes' code (`diffLines`
exported from Inspector). `CrossRunPanel` gains `overlay trajectories` → fetches each same-task
run's node metrics, builds a running-best series, and overlays them on one axis via a new monochrome
`MultiTrajectory` chart (`charts.jsx`).

### U5 · Finish the command-bar model: `#`-context and pre-routing — ✅ SHIPPED
`AssistantBar` now (a) extracts `#12` / `#node-12` refs from the input, shows them as live context
chips, and folds "the user means experiment(s) #… — read them with the run tools" into the
instruction; and (b) a cheap `preRoute` catches bare NL control phrases ("stop", "pause the run")
and fires the control event directly — no LLM round-trip. `@`-scoping is moot with the unified
assistant.

### U6 · Trust panel actions + "why" narration — ✅ SHIPPED
`TrustPanel` reward-hack rows show the enforcement mode (`trust_gate`) and a `quarantine` action
(`node_abort`) so a flagged node can be pulled from the search live, plus click-through. And a
`WhyStrip` (`ui/src/panels.jsx`, mounted above the canvas) narrates the loop's latest autonomous
decisions live — A7 strategy rationale, unified-agent action, policy why-this-node — folded from
events already logged; click a referenced node to jump to it.

### U7 · Lower-priority but real
Mobile/readonly responsive view for monitoring long runs from a phone; chart interactivity
(zoom/brush/log-scale/export on `charts.jsx`); panel consolidation (17 panels → 4 hubs: Progress /
Trust / Analysis / Knowledge); per-user attribution of steering actions in shared deployments.

---

## 6. What the 2025–26 SOTA sweep adds (external techniques worth adopting)

*(Synthesis of the parallel web-research passes over MLE-bench leaders and cross-cutting
techniques; citations in the pass reports. See §7 for the mapping into priorities.)*

**The field moved.** The official MLE-bench leaderboard (2026-02) now clusters at **61–64%
any-medal** (Famou-Agent 2.0 64.4%, AIBuildAI 63.1%, Google MARS+ 62.7%, MLEvolve 61.3%) — roughly
double the mid-2025 systems LoopLab's roadmap benchmarked against (AIRA 31.6%, R&D-Agent 30.2%).
Two drivers: frontier-model uplift (Gemini-3-Pro / Claude-Opus-4.6 scaffolds) and the technique
stack below. ML-Master 2.0 jumped 29.3%→56.4% largely on a *memory* mechanism (Hierarchical
Cognitive Caching — distilling transient execution traces into stable long-term knowledge).

Cross-system findings, mapped to LoopLab:

1. **Operators over search policy** (AIRA, confirmed by ablation: MCTS/evo over AIDE-ops ≈ no
   gain; upgraded ops = 39.6→47.7% Lite). LoopLab has most operators built — the gap is defaults
   (T1) and the run-level ablation outer loop (P3).
2. **More attempts ≫ more compute per attempt.** MLE-bench: pass@1 16.9% → pass@8 34.1%, while
   24h→100h moved 8.7%→11.8% only. Parallel independent attempts + top-k selection is the
   cheapest doubling available → prioritize T6 (fold cache) + `max_parallel` + best-of-N (C2).
3. **Final-node selection is a major loss source.** AIRA: selecting among the top-k validation
   nodes recovers **up to 75% of the val→test generalization gap** — direct evidence for T3 (B6
   holdout selection) being worth more than any new operator.
4. **Scoped/adaptive memory is the recurring winning design** — ML-Master 1.0's
   parent-plus-siblings-only context, AIRA's per-operator scoped memory, ML-Master 2.0's HCC,
   MLZero's dual semantic+episodic memory. Confirms M1/M5 as high-ROI.
5. **Knowledge grounding beats searching harder.** The top 2025 performers all inject external
   knowledge rather than expand search: AutoMind's KB of 3,237 curated Kaggle solutions (with
   same-competition exclusion against leakage), MLE-STAR's web-search-seeded drafts, DS-Agent's
   case-based retrieve–reuse–revise–retain. Confirms M2/M3/M4 + A0f web-grounded init.
6. **Ensembling as a first-class endgame** (MLE-STAR agent-proposed iterative ensembling,
   KompeteAI multi-level merge): T8 + P2's reserved endgame budget.
7. **Verifier/checker agents are the least-mature area and a differentiation opportunity** — only
   MLE-STAR ships leakage/data-usage checkers; benchmarks (not agents) enforce holdout
   discipline. LoopLab's detectors already exist; making them *gating* (T2) is near-free
   differentiation.
8. **Model routing per role is standard** (R&D-Agent o3-Researcher + GPT-4.1-Developer;
   AlphaEvolve fast-model-breadth + strong-model-depth). LoopLab has per-role/per-stage routing —
   ship presets that use it (H3).
9. **LLM-judge is unreliable for ranking ideas** — rank empirically (cheap evals/surrogate);
   keep as a hard rule for any new panel/ideation feature (E2).
10. **Steering UX**: checkpoint-fork with edited state (LangGraph Studio pattern) — largely built
    (F6); the remaining piece is the queue/board surfaces (U1/U2).
11. **Throughput + denoised evaluation is how wall-clock scaling gets fixed** (AIRA₂,
    arXiv:2603.26499): async multi-GPU worker pool for linear experiment throughput + a
    "Hidden Consistent Evaluation" protocol (same fixed splits/seeds across candidates so scores
    are comparable) + ReAct debug agents. Notably AIRA₂ argues much of the earlier "validation
    overfitting" was *evaluation noise* — a consistent-eval protocol (already in LoopLab's design
    docs, §3.6 of 02-architecture) matters as much as a holdout split.
12. **Novelty rejection before evaluation** (ShinkaEvolve): code-embedding similarity
    (threshold ~0.99) + an LLM novelty judge reject near-duplicate candidates *before* paying for
    an eval; adaptive parent sampling and a UCB bandit over an LLM *ensemble* pick where and with
    which model to generate. Direct template for T5 + P4 (bandit over operators *and* models).
13. **Evaluation cascade + artifact feedback** (AlphaEvolve/OpenEvolve): multi-stage eval
    (cheap sanity → small-scale → full) promoting only survivors, and evaluator artifacts
    (stderr, profiling, warnings) injected into the next generation's prompt. LoopLab's
    smoke/full profiles + A6 proxy scoring are the same idea — the missing piece is feeding
    *execution artifacts* back into proposals systematically.
14. **Lessons transfer across branches, not just across runs** (MARS, arXiv:2602.02660): 63% of
    applied "lessons" in its reflective-search ablation came from *cross-branch* transfer inside
    one run — supports M1/M3 and cross-lineage insight sharing during merges.

---

## 7. Prioritized plan

**Phase 1 — defaults, trust, and speed (mostly S-effort, uses what exists):**
T1 research-grade preset → T2 enforcing trust ladder → T6 fold caching (unlocks real
`max_parallel`, and the pass@k evidence says parallel attempts are the cheapest gain) → T4 real
embeddings → T5 semantic novelty → finish parked P0s (out-of-process grader, stdout redaction,
UI auth).

**Phase 2 — planning & memory depth (the differentiators):**
P1 hypothesis ledger → U2 hypothesis board → M1 operator-scoped memory → M2 task fingerprints →
M3 failure lessons → P3 run-level ablation attribution → T8 ensemble-by-default → T3 holdout gap
guard (B6) before any published benchmark.

**Phase 3 — scale & polish:**
P4 operator bandit → P2 plan/budget artifact → M4 auto-skills → U1 queue view → U3 canvas
actions → U4 diffs/overlays → T7 LLM cache → T9 sandbox limits → U7 polish.

**If you do only three things:** **T1** (turn the built intelligence on by default),
**P1+U2** (hypothesis ledger + board — converts mutation search into research and gives the UI
its missing "mind"), **M2+M3** (fingerprint-keyed lessons incl. failures — real cross-run
learning instead of exact-task warm-start).
