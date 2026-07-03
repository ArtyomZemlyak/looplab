# LoopLab — Phased Implementation Plan (2026-07-02)

> **Implementation status (2026-07-03).** Phases 1–5 are **implemented, config-first, and
> test-covered** on branch `claude/agent-systems-research-uhnl6d`. Every "best new" mechanism ships
> **ON by default** (holdout-gated promotion, ensemble-by-default merge, disjoint confirm seeds,
> stagnation-adaptive strategy, semantic novelty, weighted parents, deeper debug, failure
> reflection, operator-scoped memory, insight backprop, memory hygiene, auto-skills, scaling
> digest, research verification, workdir audit, list-wise BoN, endgame reserve); the higher-cost or
> less-evidenced knobs (operator bandit, LLM cache) default OFF and turn on via `profile: thorough`
> or explicitly. **Deliberately not flipped:** `max_parallel` and `max_nodes` stay the user's cost
> knobs (D9/G3 read-dominant parallelism is available at `max_parallel>1` but the default stays 1,
> per the existing local-first ADR — a real training run owns its resources); real MLE-bench (5.6)
> stays external-infra-gated. Each phase was committed separately with its own tests; the full
> suite is green after every phase.
>
> **Memora synergy (2026-07-03).** After merging the concurrent Memora harmonic-memory work into
> master, the cross-run **lessons** tier (D2/M3) was wired through Memora's abstractor+anchor index
> (`engine.memory.retrieve_lessons_harmonic`): lesson retrieval now gains anchor-expansion recall —
> surfacing a lesson from a differently-worded but anchor-linked task that the fingerprint-Jaccard
> gate misses — while every hit still passes the D2 contradiction/forgetting/corroboration hygiene.
> Off (byte-identical Jaccard-only) when `memora=false`; shares Memora's content-hash abstraction
> cache so no extra LLM cost. The three memory tiers now compose: canonical case write (JsonlCaseLibrary)
> → harmonic case/KB retrieval (Memora) → harmonic + hygienic lessons (both) → scoped in-run digest.

**Basis.** Synthesis of [11-agent-systems-research.md](11-agent-systems-research.md) (directions
D1–D14, fresh 3-stream evidence), the still-open items from
[10-autoresearch-improvement-research.md](10-autoresearch-improvement-research.md) /
[BACKLOG.md](BACKLOG.md) (T3/T5/T7/T8/T9/T10, P2/P3/P4, M1/M4/M5, foundation P0s), and the
code-level map (every item lands on an existing seam). Supersedes the ordering in BACKLOG §3 where
they conflict; item IDs are kept for traceability.

**Sequencing logic.** (1) Retire *active risks* first (memory misevolution exposure, label-leak
paths) — they get worse as the engine runs more. (2) Then *selection integrity* — nothing measured
afterwards is trustworthy without it, and every later phase wants to measure itself. (3) Then
*capability per compute* (search + novelty), which every long run compounds on. (4) Then *depth*
(memory/context), *verification* (research + evaluator), and *scale* — each measurable against the
now-trustworthy yardstick.

**Cross-cutting rules (every item):** config-first knob + Settings UI + `profile: thorough` wiring;
replay-safe events (recorded decisions, order-tolerant fold); a regression test per mechanism;
audit-only until the phase's exit criterion says otherwise.

---

## Phase 0 — Retire active risks *(~1–2 weeks of S/M items)*

| Item | What | Where | Effort |
|---|---|---|---|
| **0.1 · D2 memory hygiene** | Lessons gain provenance `{run_id, node_ids, fingerprint, evidence_count, last_confirmed}`; contradiction check at load (conflict with newer resolved hypothesis on matching fingerprint → quarantine); run-end consolidation (merge near-duplicates via T4 embedder, bump evidence_count); forgetting (age + refuted → prune); injection ranked by confidence×recency | `engine/memory.py`, `orchestrator.py:1252,1336` | S–M |
| **0.2 · Out-of-process mlebench grader** | Labels never importable from the candidate process (closes `mlebench.py:102`; B1 `host_score` is the primitive) | `adapters/mlebench.py`, eval loop | M |
| **0.3 · Output redaction finish** | Regex+entropy pass over `stdout_tail` before events (closes `orchestrator.py:808`) | `orchestrator.py`, `trust/` | S |
| **0.4 · T9 sandbox limits** | `resource.setrlimit` (AS/RSS) + optional `unshare -n` on the default tier | `runtime/sandbox.py` | S–M |
| **0.5 · UI auth token** | Shared secret on mutating `/api/*` + `task_file` allow-list | `serve/server.py` | S |

**Why first:** 0.1 is the one place the engine is *actively* exposed to a published failure mode
(misevolution: memory-driven reward hacking) because `reflection_priors` defaults ON; 0.2–0.5 are
the long-standing trust P0s that gate everything "credible".
**Exit criterion:** a run with `memory_dir` set cannot inject a refuted/contradicted lesson; a
candidate cannot read labels; secrets can't reach the event log; a node can't OOM the engine.

## Phase 1 — Selection integrity: the benchmark gate *(~2–3 weeks)*

| Item | What | Where | Effort |
|---|---|---|---|
| **1.1 · D1/B6 holdout-gated promotion** *(the headline change)* | Eval contract grows an optional `holdout` reader the search never sees; champion = best-on-holdout among val-top-k; optional Arbor-style `promote_margin` for merge-to-trunk semantics; `generalization_gap` per node folds into Trust panel; events `holdout_evaluated`/`promotion_gated`; default ON for `mlebench*`/`dataset` under `thorough` | eval contract, `events/replay.py:326-355`, `_confirm_phase`, Trust panel | M |
| **1.2 · T8 ensemble merge default** | `merge_mode="ensemble"` becomes the default for code-bearing kinds (code/repo/dataset/mlebench); mean-merge stays for numeric-only | `core/config.py`, `search/operators.py:16` | S |
| **1.3 · Consistent-eval enforcement** | Same folds/seeds pinned across all candidates of a run (the `trust/cv.py` harness made mandatory on code paths that still allow drift), per AIRA₂'s "evaluation noise" finding | `trust/cv.py`, adapters | S–M |

**Evidence:** Arbor's held-out merge gate at 86.4% Lite; AIRA +9–13 pp selecting on test; ensembling
= best-evidenced single lift (37.9→43.9%; −9 pp when removed).
**Exit criterion:** on the bench suite, champion selection uses holdout; val−holdout gap is
reported per run; a `looplab bench` A/B (gap-guard on/off) is recorded in the doc.

## Phase 2 — Capability per compute: search & novelty *(~2–4 weeks)*

| Item | What | Where | Effort |
|---|---|---|---|
| **2.1 · D3 stagnation-adaptive strategy** | Fold-derived stagnation signal (no best-improvement in N evals / M eval-seconds; normalized error-signature repetition — also fixes T10's exact-match stuck check); default Strategist rule: greedy → broaden (evo/MCTS or draft breadth) on stagnation, back on new leader | `events/replay.py` (signal), `engine/strategist.py` (rule) | S–M |
| **2.2 · Selective reflection** | `failure_reflection` + `deep_research` fire on the 2.1 trigger instead of fixed cadence | `orchestrator.py` | S |
| **2.3 · D4/T5 novelty-before-compute** | Reject ideas by embedding similarity vs accepted **and failed** siblings (failure surfaced to the Researcher: "you tried X, it scored Y because Z"); optional async literature verdict `novel/partial_overlap/prior_art` per node via existing web/literature tools, recorded before the Developer runs | `orchestrator.py:1407-1441`, `tools/vectorstore.py`, `agents/deep_research.py` | M |
| **2.4 · D7 weighted parent sampling** | `improve`/`merge` parents sampled ∝ softmax(fitness) × novelty bonus (greedy still exploits best for its main line) | `search/policy.py` | S |
| **2.5 · P4 operator bandit** | UCB/Thompson over per-operator yield (Δmetric per eval-second, folded from events) replacing fixed cadences; **operators only** — model-bandits are evidenced as low-value | `search/policy.py`, `events/replay.py` | M |
| **2.6 · T10 deeper debug** | `debug_depth` 1→2–3 with the A0e bounded ReAct act/observe loop | `orchestrator.py` repair path | M |

**Evidence:** FML-bench (adaptive beats all six fixed strategies; stagnation is the trigger that
matters); ShinkaEvolve ablations (#1 parent sampling, #2 novelty rejection); OPPO (reflect
selectively, not constantly).
**Exit criterion:** bench suite shows ≥X% better best-metric per eval-second vs Phase-1 baseline
(set X after the Phase-1 A/B); duplicate-idea evaluations measurably drop.

## Phase 3 — Memory & context depth *(~3–4 weeks)*

| Item | What | Where | Effort |
|---|---|---|---|
| **3.1 · D6/M1 operator-scoped memory** | draft/improve see *sibling* summaries (diversity pressure); debug sees the *ancestral repair chain* (no undo↔redo); port aira-dojo `MEM_OPS` shape | `agents/roles.py:156-180`, `events/digest.py` | S–M |
| **3.2 · D6 insight backpropagation** | On node resolution, distill a one-line lesson attached to the ancestor path (fold-derived projection, like hypotheses); future expansions from those ancestors inherit it | `events/replay.py`, digest assembly | M |
| **3.3 · M5 scaling digest** | Working-set budget scales with model context (`context_budget.py`); structure = per-branch one-liners + open hypotheses + component attribution; "preserve decisions and flags" compression rule | `events/digest.py:121` | S–M |
| **3.4 · D11 compression model slot** | Fifth per-role slot (cheap summarizer) for eval-stdout digesting, research-result compression, lesson distillation | `core/config.py`, `unified_agent.py` routing | S |
| **3.5 · M4 auto-distilled skills** | Run-end: repeatedly-winning technique → draft SKILL.md (`provenance: auto, status: candidate`; promoted after winning on a 2nd distinct fingerprint). *Gated on Phase-0 hygiene* | report stage, `agents/skills.py` | M |
| **3.6 · P3 run-level ablation attribution** | Aggregate per-component deltas (data-prep/features/model/loss/ensemble) into a run-level table; Strategist biases next batch toward highest-yield component | fold + `strategist.py` + report | M |

**Exit criterion:** operators receive scoped context (visible in traces); a 100+-node run's digest
stays informative; ≥1 auto-skill survives promotion on the bench suite.

## Phase 4 — Verified research & the evaluator arms race *(~4+ weeks, parallelizable with Phase 3)*

| Item | What | Where | Effort |
|---|---|---|---|
| **4.1 · D8 evidence ledger** | Every `ResearchMemo`/report claim carries refs (node ids / event seqs / URLs), folded as events | `agents/deep_research.py`, report stage | S–M |
| **4.2 · D8 decoupled Verifier** | Separate role (different model) checks *synthesis* claims against cited evidence; single rubric-call grading; unsupported claims flagged in the UI report | new role + report pipeline | M |
| **4.3 · D5 hacker–fixer–solver harness hardening** | Per task-kind, offline/between-runs: hacker seeks eval exploits, fixer patches detectors/harness, solver confirms legitimate solutions still pass; discovered exploits persist as a regression suite; cheap models suffice | new `trust/harden.py`, eval contracts | M–L |
| **4.4 · Sandbox instrumentation** | File-access logging + patch tracking folded into `reward_hack_suspected` evidence (RewardHackingAgents recipe) | `runtime/sandbox.py`, `trust/` | M |
| **4.5 · D12 deep-research decomposition** | Memo questions decompose into a dependency DAG; independent branches run as parallel workers (orchestrator-worker w/ explicit effort rules + artifact handoff); re-plan on results | `agents/deep_research.py` | M |

**Evidence:** Kosmos 57.9% synthesis accuracy; Aletheia's decoupled verifier; hacker-fixer-solver
62%→0% with the solver guardrail; 16% of tasks hackable; evaluator–policy co-adaptation theory.
**Positioning:** 4.1–4.3 are the "credible autonomous science" differentiators the event log makes
uniquely cheap here.
**Exit criterion:** a report ships with zero unflagged unsupported synthesis claims on the bench
suite; the exploit regression suite blocks ≥1 previously-passing hack per hardened task kind
without rejecting legitimate bench solutions.

## Phase 5 — Scale & proof *(ongoing after Phases 1–2)*

| Item | What | Where | Effort |
|---|---|---|---|
| **5.1 · D9 read-dominant parallelism** | Raise `max_parallel` for read-dominant actions only (independent drafts, deep-research workers, confirm seeds); write-dominant (improve-on-lineage, report) stays serialized; explicit effort-scaling defaults | `orchestrator.py:777-796`, policy action tagging | M |
| **5.2 · G3 distributed eval** | Worker pool (local processes → remote later) behind the same anyio seam | `orchestrator.py`, new worker module | L |
| **5.3 · D10 list-wise BoN selection** | Comparative LLM selection over N candidates where per-candidate eval is unaffordable (empirical eval still decides when available) | `search/best_of_n.py` | S–M |
| **5.4 · P2/D13 plan artifact + endgame reserve** | Explicit `plan` event: ranked backlog + per-phase budget split; reserved confirm/ensemble window at budget end | `engine/strategist.py`, fold, Queue panel | M |
| **5.5 · T7 LLM response cache** | Content-addressed `hash(model,messages)→completion` under the run dir; stable prompt prefixes for server-side caching | `core/llm.py` | S–M |
| **5.6 · Real MLE-bench run + publish** | Theme-D1 wiring (Kaggle data + official grader), **gated on Phases 0–1** (out-of-process grading + holdout selection); publish with the val/holdout gap disclosed | `adapters/mlebench.py`, infra | L |

**Exit criterion:** a published, reproducible MLE-bench (or equivalent) number with holdout
discipline and hardened grading — the external proof point.

---

## Dependency graph (critical path)

```
0.1 D2 hygiene ──────────────► 3.5 M4 auto-skills
0.2-0.4 trust P0s ───────────► 5.6 real MLE-bench
1.1 D1 holdout gate ─────────► 5.6 real MLE-bench, and the yardstick for ALL later A/Bs
2.1 stagnation signal ──► 2.2 selective reflection; feeds 5.4 plan artifact
2.3 novelty gate ◄── T4 embedder (shipped)
3.1/3.2 scoped memory ◄────── digest refactor 3.3 (do together)
4.2 verifier ◄── 4.1 evidence ledger
5.1 parallelism ◄── T6 read cache (shipped); pairs with 2.x (breadth needs cheap novelty/proxy)
```

## If capacity is one person: the minimal spine

Phase 0.1 → 1.1 → 2.1+2.3 → 3.1 → 4.1/4.2 — five S/M-effort items, each on an existing seam, that
together retire the active risk, make numbers trustworthy, adopt the two best-evidenced 2026 search
levers, and turn reports into verifiable artifacts. Everything else compounds on these.
