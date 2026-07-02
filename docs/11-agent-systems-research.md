# LoopLab — Deep Research: Agentic Systems, Deep-Research Systems & AI R&D (2026-07-02)

**Method.** Three inputs, cross-checked against everything already shipped or planned so nothing
below re-proposes known work: (1) a fresh 106-agent web deep-research pass over three streams —
*deep-research system architectures*, *agentic best practices 2025–26*, *AI-R&D frontier 2026 Q1–Q2*
— with adversarial claim verification (12 merged findings survived 3-0 votes; stream-3 items marked
◆ are primary-source-quoted but not adversarially voted); (2) a full code-level map of the current
engine (mechanism → file:line); (3) the internal evidence base
([10-autoresearch-improvement-research.md](10-autoresearch-improvement-research.md),
[ROADMAP.md](ROADMAP.md), [BACKLOG.md](BACKLOG.md), [RESEARCH_NOTES.md](RESEARCH_NOTES.md)).

**TL;DR.** The engine's engineering core is at or above open-source SOTA (event-sourced replay,
policy/operator separation, Strategist, hypothesis ledger, trust ladder). The 2026 frontier moved in
four directions LoopLab hasn't: **(1) held-out-gated promotion** is now standard in the best system
(Arbor, 86.4% MLE-bench-Lite) — our parked B6 is the single most-validated missing piece;
**(2) evaluators became adversarially co-evolved** (hacker–fixer–solver loops driving exploit rates
62%→0%) while ours are static regex; **(3) memory became a managed lifecycle** (consolidation +
forgetting + misevolution guards) while ours is append-only — and our `reflection_priors` default-ON
makes that an active risk, not a nicety; **(4) research became verified synthesis** (claim-level
provenance, decoupled verifiers) while our reports/memos are unverified generation. Plus one cheap,
well-evidenced search upgrade: **adaptive greedy→broad switching on stagnation** beats every fixed
policy — and we already have the Strategist to host it.

---

## 1. What the fresh research established (evidence)

### Stream 1 — Deep-research systems (how the best "go research X" engines are built)

- **Orchestrator–worker parallelism works, and its gains are mostly parallel token spend, not
  specialization.** Anthropic's Opus-lead + Sonnet-workers beat single-agent Opus by 90.2% on their
  internal research eval; token usage alone explains 80% of BrowseComp variance; a model upgrade
  beat doubling the token budget. Multi-agent costs ~15× chat tokens, so it only pays for
  high-value, parallelizable phases. *(anthropic.com/engineering/multi-agent-research-system; 3-0)*
- **Effort-scaling rules must be explicit.** Agents can't judge appropriate effort: Anthropic embeds
  rules in prompts (simple fact → 1 agent / 3–10 tool calls; comparison → 2–4 subagents;
  complex → 10+), which double as the stopping criterion. Subagents need an objective, output
  format, tool guidance, and hard task boundaries or they duplicate work. *(ibid.; 3-0)*
- **Read/write distinction reconciles "multi-agent" vs "don't build multi-agents".** Parallelize
  *read-dominant* work (search, literature, independent experiment branches); keep *write-dominant*
  work (code evolution, report writing) single-threaded, because parallel writes embed conflicting
  implicit decisions. *(LangChain synthesis of Cognition + Anthropic essays; 3-0)*
- **Context compression is an explicit architectural stage.** LangChain's open_deep_research (top
  open-source reference) has four model slots — cheap summarizer for raw results, research model,
  compression model before synthesis, report writer. Two abandoned architectures (plan-and-execute,
  supervisor multi-agent) are preserved in-repo labeled "less performant". Swapping GPT-4.1→GPT-5
  moved its benchmark score more than any architecture change. *(github open_deep_research; 3-0)*
- **One-shot parallel query decomposition fails on dependent sub-queries** — tree/DAG decomposition
  (decompose, run local parallelism, re-plan on results) is the surveyed winner. Deep-research
  systems converge on four components: query planning → information acquisition → memory
  management → answer generation, with an **evidence ledger** for tool provenance and citation
  grounding. *(arXiv:2512.02038 systematic survey; 3-0)*
- **Filesystem-artifact handoff beats message-passing** — subagents write outputs to storage and
  pass lightweight references to the coordinator, avoiding "game of telephone" loss. For grading
  free-form research output, a **single rubric-prompt LLM call** (accuracy / citation accuracy /
  completeness / source quality / efficiency → 0–1 + pass-fail) was more consistent than an
  ensemble of component judges. *(Anthropic; 3-0)*

### Stream 2 — Agentic best practices

- **Test-time scaling for agents: simple Best-of-N wins.** On GAIA, BoN beat beam- and tree-search
  variants (63.03 best overall); **list-wise LLM selection** (candidates compared together) beat
  independent per-candidate scoring by ~3 pts and beat majority voting. **Selective reflection**
  (only at steps where the agent is doing poorly) helps; reflecting at every step doesn't.
  *(arXiv:2506.12928; 3-0)*
- **Agent memory is a lifecycle, not a store.** The Dec-2025 Fudan/Tsinghua survey treats
  **consolidation and forgetting as first-class operations** (Forms / Functions / Dynamics
  taxonomy): an append-and-retrieve store is explicitly insufficient. *(arXiv:2512.13564; 3-0)*
- **"Self-baking" and context collapse.** Raw interaction context should be abstracted into
  persistent structured knowledge (NL summary / fixed schema / embeddings), but over-compression
  loses decisions and flags — compression must preserve decisions, flags, key facts.
  *(context-engineering survey, arXiv 2025-10; 3-0)*
- **Misevolution ◆ (ICLR 2026, arXiv:2509.26354).** Self-evolving agents degrade through four
  pathways — model, **memory**, tool, workflow. Cross-run memory accumulation causes
  *deployment-time reward hacking without any weight update* (agents repeat actions that merely
  correlated with past positive feedback); a memory-evolving coding agent lost 55% refusal rate
  over cycles. No validated fix exists yet — provenance, quarantine and pruning are the proposed
  mitigations.

### Stream 3 — AI R&D / autonomous science frontier ◆ (primary-source-quoted)

- **Arbor (arXiv:2606.11926, June 2026) — the new reference design.** Coordinator (long-lived,
  owns a persistent Idea Tree, cycle = Observe → Ideate → Select → Dispatch → **Backpropagate** →
  Decide) + short-lived Executors, one hypothesis each, isolated git worktrees. Three mechanisms to
  note: **(a) promotion is held-out-gated** — executors iterate on a dev split; a change merges to
  trunk only if it clears a configurable margin on a *protected held-out test split*
  (`merge_threshold`); **(b) insights are backpropagated up the tree** so ancestors and future
  ideas inherit lessons; **(c) a background literature/novelty check vets every new tree node**
  (novel / partial-overlap / prior-art) *before* compute is spent. Reports **86.36% any-medal on
  MLE-bench Lite** (GPT-5.5) and beats Claude Code / Codex as a single generalist controller across
  six task families at equal compute.
- **FML-bench controlled study (arXiv:2605.17373, May 2026).** Greedy hill-climbing and best-first
  tree search reach near-identical top-tier performance; which wins depends on
  *improvement-opportunity density* (dense → greedy, sparse → broad). An **adaptive agent that
  starts greedy and switches to broader exploration on stagnation beats all six fixed-strategy
  agents**. Early convergence and directionally-focused exploration predict outcomes; solution
  diversity, token cost, and wall-clock don't.
- **ShinkaEvolve ablation ranking (arXiv:2509.19349, ICLR 2026).** Sample-efficiency (SOTA circle
  packing in ~150 evals vs thousands) comes, in order of measured impact: **(1) fitness+novelty-
  weighted parent sampling** (beats both hill-climb and random), **(2) code-novelty rejection
  before evaluation** (embedding sim + LLM novelty judge), (3) bandit-over-LLM-ensemble (only
  slight). Archive/parent policy and duplicate filtering matter more than model routing.
- **Co-evolving evaluators got operationalized (arXiv:2606.08960, June 2026).** 16% of 1,968
  audited benchmark tasks are reward-hackable from the task description alone. A
  **hacker–fixer–solver loop** (hacker seeks exploits, fixer patches the harness, solver confirms
  legitimate solutions still pass) drove attack success on KernelBench 62%→0% — and the **solver
  guardrail is load-bearing**: hacker+fixer alone over-harden (legitimate pass rate 0–22%, restored
  to 92–98% by solver-driven autopatch). Cheap models suffice to harden against stronger attackers.
  Companion instrumentation recipe (RewardHackingAgents, arXiv:2603.11337): per-episode workspaces,
  patch tracking, file-access logging, trusted reference metrics, explicit trust regimes; note
  **LLM-judge hack detectors are gullible** on smaller models.
- **Reward-hacking theory (arXiv:2604.13602).** Hacking = objective compression × optimization
  amplification × **evaluator–policy co-adaptation**: as the searched policy distribution drifts,
  a static evaluator leaves its reliable region — the evaluator must update *alongside* the search.
  For agentic loops, the primary mitigation class is dense process supervision (per-step signals),
  not output-only checks. SpecBench adds: hacking stays near zero only below a complexity
  threshold — risk grows exactly as tasks get harder.
- **Kosmos (arXiv:2511.02824; 3-0 verified).** 12-hour runs, ~200 rollouts, coherence via a
  persistent **structured world model** shared by the data-analysis and literature agents; every
  report claim linked to executed code or a paper. Expert eval: data-analysis statements 85.5%
  accurate, literature 82.1%, **cross-evidence synthesis only 57.9%** — synthesis, not analysis,
  is the failure mode. Perceived research value scaled roughly linearly up to 20 cycles.
- **Aletheia (Google, 2026-02) ◆.** Research-level math via generate→verify→revise **with the
  verifier decoupled from the generator** ("essential for identifying flaws the model
  overlooked"); the flagged→confirmed funnel was 212→13 on Erdős problems — verification is the
  throughput bottleneck, and only 6.5% of "technically correct" solutions were *meaningfully*
  correct.
- **AIRA2 (arXiv:2603.26499) ◆.** Long horizons keep paying: mean percentile 71.8%@24h → 76.0%@72h
  on MLE-bench-30 once operators/eval/budget bottlenecks are fixed — throughput and consistent
  evaluation, not policy cleverness, gate long-run scaling (consistent with internal notes §6.11).

---

## 2. Where the engine stands against this (verified in code)

Strengths that now look *ahead* of most published systems: event log + pure fold
(`events/replay.py`) giving free provenance for every claim; policy/operator separation with five
policies (`search/policy.py:401`); legal-action envelope + LLM pilot (`search/policy.py:361`,
`agents/unified_agent.py:117`); Strategist meta-controller; derived hypothesis ledger
(`events/replay.py:361`); trust ladder `audit|gate|block` (T2); external-agent patch gate
(`agents/cli_agent.py:220`); per-role/per-stage models (H3). The agentic-science surveys' own
checklists (five capabilities, four processes, reproducibility-by-replay) are essentially satisfied.

The gaps, restated against the new evidence:

| # | Gap (code reality) | 2026 evidence against it |
|---|---|---|
| G1 | Champion/merge selected purely on the search metric; no holdout reader (`replay.py:326-355`; B6/T3 parked) | Arbor's held-out `merge_threshold` at 86.4% Lite; AIRA +9–13 pp selecting on test; AIRA val-test gap 15–16.6% |
| G2 | Trust detectors are static regex/heuristics (`trust/reward_hack.py`, `leakage.py`), audit-only by default | 16% of tasks hackable; hacker–fixer–solver loops 62%→0%; evaluator must co-evolve with the search |
| G3 | Cross-run memory is append-only; `reflection_priors` **defaults ON**; lessons keyed by fingerprint with no contradiction/pruning path (`engine/memory.py`, `orchestrator.py:1252,1336`) | Misevolution: memory accumulation → deployment-time reward hacking, 55% refusal-rate loss; memory surveys make consolidation+forgetting first-class |
| G4 | Fixed policy per run; greedy always exploits the single global best (`policy.py:70-141`); no stagnation signal | FML-bench: adaptive greedy→broad on stagnation beats all fixed strategies |
| G5 | Parent selection = global best (greedy) or rotating elites (evo); no novelty/fitness-weighted sampling | ShinkaEvolve ablation #1 lever |
| G6 | Novelty gate = L2 over numeric params, *jitters* instead of rejecting (`orchestrator.py:1407-1441`); no idea-level or literature-level novelty check before compute | ShinkaEvolve rejection-before-eval (ablation #2); Arbor per-node novelty verdicts before dispatch |
| G7 | Deep-research stage is a single agent (`agents/deep_research.py`); memos and the final report are unverified generation; no per-claim provenance | Kosmos 57.9% synthesis accuracy; Aletheia decoupled verifier; evidence-ledger pattern; Anthropic orchestrator-worker + rubric judge |
| G8 | Flat 1200-char digest for every operator (`events/digest.py:121`); no sibling/ancestral scoping (M1); no in-run lesson propagation | Arbor Backpropagate; MARS cross-branch lessons; "self-baking" with context-collapse guardrails |
| G9 | `best_of_n` selection is a static execution-free score (`search/best_of_n.py`); reflection cadence fixed | List-wise comparative selection +~3 pts; selective (stagnation-triggered) reflection |
| G10 | `max_parallel=1` default; deep-research not parallelized | Token-throughput explains 80% of research-eval variance; pass@8 ≈ 2× pass@1; AIRA2 71.8→76.0 over 72h |

---

## 3. Improvement directions (prioritized, concrete)

### P0 — do next; each is validated by ≥2 independent 2026 sources

**D1 · Un-park B6 as *holdout-gated promotion* (Arbor-shape, not just a report column).**
The original B6 framing was "report a generalization gap". The 2026 evidence upgrades it to a
*promotion gate*: iterate on dev, **merge/champion only on a held-out margin**.
*Concrete:* eval contract grows an optional `holdout` reader the search never sees; promotion to
champion (and optionally `merge` parent selection among val-top-k) requires
`holdout_delta ≥ promote_margin`; per-node `generalization_gap` folds into the Trust panel; events
`holdout_evaluated` / `promotion_gated` (replay-safe). Default ON for `mlebench*`/`dataset` kinds
under `profile: thorough`. *Touches:* eval contract, `replay.py` best-selection, confirm phase.
*This gates any publishable benchmark number and is the highest-validated single change.*

**D2 · Memory hygiene before memory growth (misevolution guards).**
`reflection_priors` is now default-ON — the exact pathway the misevolution paper shows corrupting
agents. Before growing memory further (M4 auto-skills), add the lifecycle:
- every lesson carries provenance `{run_id, node_ids, fingerprint, evidence_count, last_confirmed}`;
- **contradiction check** at load: a lesson whose claim conflicts with a *newer* resolved hypothesis
  on a matching fingerprint is quarantined, not injected;
- **consolidation pass** at run end: merge near-duplicate lessons (T4 embeddings exist), increment
  evidence counts instead of appending clones;
- **forgetting**: age- and refutation-based pruning (a lesson refuted by a later run is dropped);
- cap injected lessons by *confidence × recency*, not just top-5 similarity.
*Touches:* `engine/memory.py`, `_write_reflection_note` / `_load_reflection_priors`
(`orchestrator.py:1252,1336`). Cheap, and it converts M2/M3 from a liability into the moat.

**D3 · Adaptive policy switching on stagnation (the FML-bench result, hosted in the Strategist).**
Add a folded **stagnation signal** (no best-improvement in N evals / M eval-seconds; normalized
error-signature repetition) and a default Strategist rule: start greedy; on stagnation switch to
evolutionary/MCTS breadth (or raise draft breadth); on new leader, switch back. The Strategist
(`engine/strategist.py`) already has the seam and the `strategy_decision` audit event — this is a
rule + one derived signal, not new machinery. *Also satisfies "selective reflection": fire
`failure_reflection` and Deep-Research on the same trigger instead of a fixed cadence.*

**D4 · Novelty rejection *before* compute (upgrades T5, adds Arbor's literature verdict).**
Two-layer gate at idea acceptance: (1) embedding similarity of `idea.approach+rationale` vs
accepted *and failed* siblings (T4 embedder exists) — near-duplicates of failures are rejected with
the failure surfaced to the Researcher; (2) optional background **novelty check** via the existing
literature/web tools writing `novel | partial_overlap | prior_art` into the node before the
Developer runs (network-optional, async so it never blocks the loop). Events: `novelty_rejected`
(exists), `novelty_verdict` (new). *ShinkaEvolve ablation ranks this above model routing.*

### P1 — differentiators; the engine's seams make these cheaper for us than for competitors

**D5 · Co-evolving evaluator: a hacker–fixer–solver loop over the eval harness.**
Periodically (per task-kind, offline or between runs) run three roles against the task's eval
contract: hacker tries to pass without solving; fixer patches the harness/detectors; **solver
verifies legitimate solutions still pass** (the load-bearing guardrail). Persist discovered
exploits as a regression suite (the "co-evolving evaluator" idea from the exploration doc, now with
a published, working recipe and numbers). Start with `mlebench`/`dataset` kinds; cheap models
suffice. Add the RewardHackingAgents instrumentation to the sandbox: file-access logging + patch
tracking folded into `reward_hack_suspected` evidence. *Upgrades G2 from static regex to an
arms-race asset; pairs with T9 (out-of-process grader).*

**D6 · Insight backpropagation + operator-scoped memory (M1, extended).**
Implement A0c (draft/improve see *sibling* summaries; debug sees the *ancestral repair chain*) —
and add Arbor's step: when a node resolves, distill a one-line lesson and attach it to the
*ancestor path* so future expansions from those ancestors inherit it. This is a fold-derived
projection (like hypotheses) — no new writes needed beyond a `lesson` field on existing events.
Replaces the flat digest for operators; the digest itself scales with `context_budget` (M5) and
follows the "preserve decisions and flags" compression rule.

**D7 · Weighted parent sampling as a policy upgrade.**
Add ShinkaEvolve-style fitness+novelty-weighted parent sampling: greedy keeps exploiting the best,
but `improve`/`merge` parents are sampled ∝ softmax(fitness) × novelty bonus instead of always
`state.best()` / rotating elites. Small change in `policy.py`; measurable via `looplab bench`.

**D8 · Verified synthesis: decouple a Verifier from report/memo generation.**
Kosmos's 57.9% synthesis accuracy is the warning; the event log is our unfair advantage. (a) Every
claim in `ResearchMemo` and the final report carries **evidence refs** (node ids / event seqs /
URLs) — an evidence ledger, folded like everything else; (b) a separate Verifier pass (different
role, ideally different model) checks each *synthesis* claim against its cited evidence and flags
unsupported ones — single rubric-call grading, not a judge ensemble; (c) unsupported claims render
flagged in the UI report. This is Aletheia's decoupled-verifier shape applied where the failure
data says it matters (cross-evidence claims), not everywhere.

**D9 · Parallelize the read-dominant phases (and only them).**
The read/write rule gives a principled parallelism map: parallel **drafts** (independent branches),
parallel **deep-research workers** (orchestrator-worker with explicit effort rules: simple question
→ 1 worker, comparison → 2–4, broad survey → more; hard task boundaries per worker; artifacts to
the run dir, references into events), parallel **confirm seeds** — while `improve` on a lineage and
report writing stay single-threaded. Raise `max_parallel` for the read-dominant actions only.
*Pass@k and token-throughput evidence both say this is the cheapest capability doubling; T6's read
cache already removed the IO ceiling.*

### P2 — worthwhile, after the above

**D10 · List-wise Best-of-N selection.** Where an empirical eval isn't affordable per candidate,
replace `best_of_n.py`'s static scoring with a *list-wise comparative* LLM selection over the N
candidates (evidenced +~3 pts over pointwise; keeps the internal rule "LLM ranks only as a prior —
empirical eval decides" for anything that does get evaluated).

**D11 · Compression model slot.** A fifth per-role slot — a cheap summarizer/compressor used for
digesting eval stdout, research results, and lesson distillation (open_deep_research's four-slot
pattern; pairs with M5/H3, one config key + routing).

**D12 · Deep-research query planning as tree/DAG.** When the memo question decomposes, decompose it
explicitly (sub-queries with dependencies), run independent branches parallel (D9), re-plan on
results — instead of one-shot single-agent research. *(Survey-backed; moderate effort.)*

**D13 · Endgame budget reserve (P2 from doc 10, sharpened).** Keep: explicit `plan` event with
phase budget split and a reserved confirm/ensemble window. New evidence framing: FML-bench's
"early convergence predicts outcomes" + AIRA2's long-horizon gains both reward disciplined budget
phasing over open-ended exploration.

**D14 · Fold in the remaining already-tracked items** unchanged in priority: T8 ensemble-as-default
merge for code tasks, P3 run-level ablation attribution, P4 operator bandit (note: ShinkaEvolve
ranks bandit-over-models *last* — do P4 for operators, skip model-bandits), T7 LLM response cache,
T9/T10 sandbox limits + deeper debug, D1(real MLE-bench) after G1/D1 land.

---

## 4. If you do only three things

1. **D1 — holdout-gated promotion.** The field's best system ships it as a merge gate; every
   benchmark claim is ungated without it; the eval-contract seam is one reader away.
2. **D2 — memory hygiene.** `reflection_priors` default-ON without contradiction-checking and
   forgetting is the one place the engine is actively exposed to a *documented, ICLR-published*
   failure mode. Small diff, big risk retired.
3. **D3+D4 — stagnation-adaptive Strategist + novelty-before-compute.** Both are one-signal
   additions to existing seams (Strategist rules; T4 embedder), and both are the top-ranked levers
   in the two strongest 2026 ablation studies (FML-bench, ShinkaEvolve).

**Positioning note.** The event log makes D8 (verified synthesis with claim-level provenance) and
D5 (co-evolving evaluator with an exploit regression suite) *cheaper for LoopLab than for any
competitor* — provenance and replay are already free. Those two, plus D1, are the credible-science
differentiators; D3/D4/D7/D9 are the capability-per-compute levers.

---

## 5. Source register (fresh pass)

Verified 3-0: Anthropic multi-agent research system (engineering blog, 2025-06);
Kosmos (arXiv:2511.02824); Deep-Research systematic survey (arXiv:2512.02038); Zhejiang DR survey
(arXiv:2506.12594); LangChain open_deep_research (repo); OPPO agent test-time scaling
(arXiv:2506.12928); Fudan/Tsinghua agent-memory survey (arXiv:2512.13564); context-engineering
survey (2025-10).

◆ Primary-source-quoted (fetched, not adversarially voted — re-verify before external citation):
Arbor (arXiv:2606.11926); FML-bench study (arXiv:2605.17373); ShinkaEvolve (arXiv:2509.19349);
hacker–fixer–solver harness hardening (arXiv:2606.08960); RewardHackingAgents (arXiv:2603.11337);
reward-hacking survey (arXiv:2604.13602); SpecBench (arXiv:2605.21384); Misevolution
(arXiv:2509.26354, ICLR 2026); Aletheia (Google, 2026-02); AIRA2 (arXiv:2603.26499);
CodeEvolve (arXiv:2510.14150).

Known-stale caution: several 2026 arXiv IDs surfaced by live search post-date training data —
resolve before quoting externally (same caveat as RESEARCH_NOTES.md).
