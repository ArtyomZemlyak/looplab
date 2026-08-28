# 56 — Where arm B's budget actually goes, and what that says about the loop's shape

2026-08-28. Six finished probes, $15.11 of real spend, measured from `attributes.cost` on
`generation` spans rather than estimated from token counts.

## 1. Two thirds of the money never writes code

| phase | $ | share | generations |
|---|---|---|---|
| `deep_research` | 4.16 | 27.5 % | 495 |
| `plan_step` | 3.73 | 24.7 % | 879 |
| `propose` | 3.44 | 22.8 % | 703 |
| `plan` | 1.15 | 7.6 % | 267 |
| `hyp_prioritize` | 0.75 | 4.9 % | 152 |
| `repropose` | 0.57 | 3.8 % | 221 |
| `foresight_rank` | 0.35 | 2.3 % | 128 |
| rest (`concept_coverage`, `lessons_distill`, `report`, `strategist_consult`, `novelty`) | 0.87 | 5.8 % | 210 |

Writing code (`plan` + `plan_step`) is **$4.88, 32 %**. Search scaffolding is **$10.22, 68 %**.
The reference agent spends essentially all of its budget on read → edit → evaluate and took
**338.26 on test for $0.51** (doc 55). The gap with arm B is a gap in the shape of the loop.

## 2. The same 68 % is also time, because the loop is LLM-bound, not machine-bound

`duration_s` summed per span name. Local evaluation is 3–5 minutes out of a 100–254 minute run:

| probe | wall | LLM | evaluation | tools | LLM share |
|---|---|---|---|---|---|
| dsFB | 148 m | 135 m | 4 m | 9 m | 91 % |
| dsFB2 | 103 m | 96 m | 3 m | 6 m | 93 % |
| dsFB3 | 123 m | 116 m | 5 m | 4 m | 94 % |
| dsNew | 144 m | 133 m | 4 m | 10 m | 93 % |
| dsNew2 | 100 m | 86 m | 3 m | 14 m | 86 % |
| ctlEdge | 254 m | 248 m | 5 m | 6 m | 97 % |
| gpt56luna | 192 m | 190 m | 15 m | 3 m | 99 % |
| sol10 | 161 m | 149 m | 20 m | 14 m | 93 % |

Parallelising evaluation cannot buy back time that is not spent there. Cutting scaffolding can.

## 3. The open probe this justifies

`dsNoDR` — deepseek-v4-flash, edge_expansion, $1, lane 33-43+81-91, launched 03:10 with
`LOOPLAB_DEEP_RESEARCH_EVERY=-1` and `LOOPLAB_CONCURRENT_RESEARCH=0`, both **confirmed in the
run's own `config.snapshot.json`**, not assumed from the environment. Everything else identical.
Controls already on the same ruler: dsNew 28.3355, dsNew2 28.3427, ctlEdge 27.5964 — a spread of
under 3 %, which is the tightest control set this campaign has. If dsNoDR lands inside it, ~28 %
of budget and wall-clock is dead weight and can be moved to code-writing.

## 4. Every zero is a real solver failure. None is a ruler refusal

Seven zero-metric nodes across all probes. All have `eval_seconds` of 35–70 s, so the
evaluation genuinely ran; a ruler refusal shows ~0.1 s.

| probe | node | reason |
|---|---|---|
| dsFB2 | 1 | `evaluator_error` — unexpected results format |
| dsFBHull | 1 | `evaluator_error` — critical execution error |
| glm53f | 2 | wrong answer — Edge expansion mismatch |
| gpt56luna | 0, 6, 10 | wrong answer — Edge expansion mismatch |
| sol1 | 0 | `no_valid_speedups` |

Four of the seven are one bug, and the diagnostics name it: `Proposed=6.2705,
Reference=12.5399`, `Proposed=4.1215, Reference=8.1821`, `Proposed=6.9556,
Reference=14.2341` — proposed is exactly half, the undirected-edge double count.

## 5. Two diagnoses of mine, refuted by measurement

* **"The failure reason never reaches later prompts."** False. Resolving the
  `input_carry`/`input_from` chain, **402 of 868 generations** carry
  `Edge expansion mismatch` with the Proposed/Reference numbers, and
  `hyp_prioritize` renders it as a `metric_account` line on experiment #0.
  gpt56luna repeated the bug at nodes 6 and 10 with that text in front of it.
  (Corrected 2026-08-28 04:3x: this was first reported as 155, undercounted by the resolver
  defect in section 11. The conclusion — the reason DOES reach later prompts — is unchanged and
  is now stronger.)
* **"The loop branches from invalid parents, which AlgoTuner's snapshot prevents."** False.
  Of 40 nodes with a parent, **0** had a parent whose metric was 0.0.

AlgoTuner does have a mechanism we lack — `Snapshot not saved - invalid solutions present`
(`AlgoTuner/utils/message_writer.py:735`) — but the outcome it buys, never building on an
invalid state, we already have by another route.

## 6. Published champions for edge_expansion are not what the earlier framing assumed

Of the three held in `.foreign_results_held/`: **Claude Opus 4.6** (28 lines) and **GPT-5.4**
(27 lines) `import networkx` and call it — the reference implementation, no optimisation.
Only **Gemini 3.1 Pro Preview** ships an extension (`solver_cy.pyx` + `setup.py`). Our dsFB3
champion is a numba `@njit` CSR kernel warmed at import time, structurally the same class as
the one champion that optimised at all. No published scores are on disk, so this compares
approach only, not numbers.

## 7. Ruler caveat on this document's own instruments

Two of the scripts producing the tables above were wrong on their first run and were fixed
before the numbers were taken: spans carry `duration_s` and `start`, not `duration_ms` and
`start_time` (the first pass reported every phase as 0.0 minutes), and `extract_champion.py`
needs LoopLab's interpreter, not AlgoTune's venv (`ModuleNotFoundError: No module named
'looplab'`). The cost column was never affected — it reads `attributes.cost`.

---

## 8 — Added 2026-08-28 04:0x: at $1 the search does not beat its own first attempt

Champion node against node 0, every probe with at least two evaluated nodes (18 of them):

| nodes | probes | champion is node 0 |
|---|---|---|
| 2–4 | 13 | **10** |
| ≥7 | 5 | 1 |

**Median improvement of the whole search over its own first node: 0.0 %.** Where it does pay it
pays enormously — sol10 20.3637 → 261.1071 over 14 nodes (+1182 %), solHull 0.9449 → 28.0845 over
11 (+2872 %), plus glm53f (10 nodes) and gpt56luna (11) — and every one of those had ten or more
nodes. A $1 ceiling buys 2–4, and at 2–4 the scaffolding is paid for and never used.

This reframes section 1. The scaffolding is not obviously waste; it is waste **at this node count**,
because it consumes the money that would have bought the nodes it needs to pay off. It also explains
the reference agent's shape directly: its iteration is one message inside one session, not a node
with propose / plan / deep_research / foresight around it, so $0.51 bought it 35 evaluations.

## 9 — And the 32/68 split is a corpus average, not a law

The two probes that finished on 2026-08-28 03:5x sit at the opposite ends of it:

| probe | task | $ | nodes | code / scaffolding | champion |
|---|---|---|---|---|---|
| dsFB4 | edge_expansion | 1.001 | 3 | **25 % / 75 %** | node 0, 28.0579 |
| dsFBKc2 | kcenters | 1.002 | 1 | **73 % / 27 %** | node 0, 33.9209 |

dsFB4 spent 75 % on search and never beat its first node in three tries. dsFBKc2 spent 69 % of the
whole run on ONE node's `plan_step` sessions and never reached a second node. Both land on node 0.
The corpus figure of 32/68 in section 1 stands as an aggregate; it is not a per-run constant, and a
fix aimed at it has to be judged on node count and final metric, not on the ratio.

## 10 — 23 orphaned workers, and what they did NOT do

Lane 2 held 23 `multiprocessing.spawn` workers with `ppid=1`, started 2026-08-25 14:01 — two and a
half days old, 0.0 % CPU, `futex_wait`, 5.1 GB RSS between them. Tempting to blame dsFB4's numbers,
which ran on that lane: measured instead, 5.1 GB against 755 GB total with 714 GB available, so
there was no memory pressure and the ruler was not dirtied. They were removed by explicit PID after
excluding this shell's own ancestry and the live `looplab.cli run` PIDs — never `pkill -f`. No
orphan newer than six hours exists, so today's probes leak nothing; this was campaign residue, and
`rerun_arm_a.sh::leg` already clears the forkserver half of it at launch.

---

## 11 — The instrument that produced sections 5 and 8 was itself wrong

`core/tracing.py:634` writes a chained turn as `input=cur[np:]`, `input_carry=np`, `input_from=<parent>`
— so `input_carry` is an **integer prefix length**, not a list, and `input` is only the SUFFIX. My
ad-hoc resolver returned as soon as it saw `input`, so for the 67 of 81 chained generations in dsBud
it read the delta and called it the prompt. Measured cost of that:

| claim | reported | true |
|---|---|---|
| dsBud step prompts carrying the budget line | 3 / 32 | **38 / 38** |
| dsFB step prompts carrying the feedback block | 49 / 125 | **90 / 125** |
| gpt56luna generations carrying the failure reason | 155 / 868 | **402 / 868** |

Every conclusion drawn from those counts was directionally right and numerically wrong. One claim
was re-checked rather than assumed: "0 of 317 dsFB3 generations carried a spend figure", which
justified the budget fix, **survives** the corrected resolver — the 16 hits for "remaining" are all
the model's own prose about optimisation headroom, none about money.

The resolver now lives in `benchmarks/algotune/span_input.py` with
`tests/test_span_input_resolution.py` beside it, so this is measured once and correctly.

---

## 12 — On kcenters the search does not merely fail to help, it degrades

Four probes, one ruler, `extract_champion` for the champion in each:

| probe | switch | $ | nodes | node scores | champion |
|---|---|---|---|---|---|
| dsFBKc | on | 1.006 | 2 | 174.36 → 124.32 | node 0 |
| dsFBKc2 | on | 1.002 | 1 | 33.92 | node 0 |
| dsKcCtl | off | 1.017 | 2 | 90.83 → **0.0** | node 0 |
| fxKcenters | off | 1.003 | 3 | 40.09 → 5.01 → 22.23 | node 0 |

Every child is worse than its parent, four runs out of four, and one child is invalid outright.

**Corrected 2026-08-28 06:2x — it is now four of FIVE.** `dsKcRep`, launched to reproduce this,
went the other way: node 0 = 10.5804, node 1 = **47.2271**, a 4.5x improvement. So "every child is
worse" was a four-run coincidence rather than a law, and the honest statement is the one section 8
already makes — the search usually fails to improve, and on kcenters it had also happened to
degrade. What survives unchanged is the spread of node 0 itself, which this probe widens rather
than narrows: 10.58, 33.92, 40.09, 90.83, 174.36, 185.67 — a factor of eighteen.
The step-feedback switch changes nothing here: on and off both land on node 0.

Note also the spread of node 0 itself — 33.92, 40.09, 90.83, 174.36, a factor of five for the same
model on the same task at the same budget. The first node is a lottery ticket and the search never
improves the ticket it drew. dsKcCtl's own shape was 67 % code / 33 % scaffolding over 113
`plan_step` generations, so this is not a run that was starved of writing time.

---

## 13 — dsNoDR: turning the exploration off freed a quarter of the budget and bought nothing

Section 3 predicted that if dsNoDR landed inside the control band, the scaffolding is dead weight
that can be moved to code-writing. It landed just below it, and the reason is more interesting than
the number.

| probe | deep_research | $ | nodes | code / scaffolding | test |
|---|---|---|---|---|---|
| dsNew | 13.0 %, 38 gens | 1.005 | 3 | 54 / 46 | 27.797 |
| dsNew2 | 12.9 %, 41 gens | 1.005 | 3 | 59 / 41 | 28.1491 |
| ctlEdge | 17.5 %, 54 gens | 1.006 | 4 | 27 / 73 | 27.7371 |
| **dsNoDR** | **$0.000, 0 gens** | 1.014 | **3** | **66 / 34** | **27.192** |

The knob worked — zero deep-research generations, confirmed in the run's own config snapshot. Code
share rose from ~54 % to 66 %. **Node count did not move (3, the same as two of three controls) and
the test number did not improve.** The freed money went into LONGER `plan_step` work inside the same
three nodes (51 % of the run), not into more nodes.

So the hypothesis in section 3 is REFUTED as stated. Node count at $1 is not limited by what
exploration takes; a node simply expands to consume whatever is available. There is no per-node
budget in `Settings` — `max_nodes` and the run-level `llm_budget_usd` are the only dials — so
freeing money anywhere just makes the current node more expensive.

Section 8 remains the live thread, and sol10 is its clearest evidence in the other direction:
$10 bought 14 nodes, the champion was node 10, and it is worth **285.58 on test**.

## 14 — Ruler noise is 10 % at high speedups, not the ~2 % this campaign has been quoting

sol10's champion, re-extracted and re-scored on the same test subset on 2026-08-28: **285.5765**,
against the **259.677** recorded in its `final.json`. Same solver, same subset, same lane width —
a 10 % spread. The ~2 % figure quoted since doc 55 came from a pair at low speedup (2.1766 vs
2.2244) and does not generalise upward.

Consequence for every comparison in this document: a single-probe difference under ~10 % at these
magnitudes is not a result. The control band on edge_expansion (27.737 / 27.797 / 28.149) is tight
only because those numbers are small.

## 15 — One published artefact was stale; the published NUMBER was not

`model-probes/sol10/champion_solver.py` on disk was written 2026-08-27 15:58 by the pre-fix
extractor, single-file, and its `import _edge_expansion` had no companion beside it. Re-scoring it
gives `speedup: 0.0, eval_seconds: 3.5` with `reason: solver_unloadable` — and 3.5 s against a
normal 40-150 s is the ruler-refusal signature, not a solver failure. Swept every probe for the
combination "0.0 test with eval_seconds < 10": **sol10 is the only one**. ds3's 0.0 sits at 35.5 s
and is a genuine failure on a data shape absent from train.

The alarm was half wrong and that half matters: `final.json` already carried 259.677 from the
post-fix re-score, so the published figure was never 0.0 — only the on-disk artefact and the tail
of `probe.log` were stale. Both are now regenerated (`champion_solver.py` + `_edge_expansion.pyx` +
`setup.py`), with the stale file kept as `champion_solver.stale.py`.

---

## 16 — The `check` command is probe-verified, by mechanism rather than by metric

`dsChkKc` is the first run on a card carrying `check` (9fa706df). `dsKcRep` is the same model, same
task, same week, on the card without it.

| probe | card | `check` | `eval_train` | `profile` |
|---|---|---|---|---|
| dsChkKc | with | **7** | 3 | 2 |
| dsKcRep | without | — | 11 | 5 |

The agent reaches for the cheap check MORE than the expensive one, which is the behaviour change
the command was built for. Three of the seven calls caught an invalid state before the engine ever
ran, verbatim: `Solution is not optimal. Found value: 132.996, Optimal value: 31.543` /
`130.754 vs 27.746` / `85.702 vs 33.999`. Against the old card's ratio — 55 free-form probes, one
of which called `is_solution` (section 12's table) — that is the hole closing.

WHAT IS NOT CLAIMED. dsChkKc's node 0 is 185.6706, the highest in the kcenters family, and that is
NOT evidence: node 0 on this task ranges 10.58 to 185.67 across six runs, so a single high draw
proves nothing about the metric. The verified claim is narrow and mechanical — the command exists,
the model prefers it, and it catches real invalidity early. Whether that converts into score needs
the run to finish and needs replicates.

---

## 17 — dsChkKc finished: the best kcenters number in the corpus, and still node 0

| probe | card | $ | nodes | node scores | test |
|---|---|---|---|---|---|
| **dsChkKc** | **with `check`** | 1.004 | 2 | 185.67 → 140.40 | **171.1507** |
| dsFBKc | without | 1.004 | 2 | 174.36 → 124.32 | 162.1315 |
| dsKcCtl | without | 1.017 | 2 | 90.83 → **0.0** | 84.6247 |
| dsKcRep | without | 1.005 | 2 | 10.58 → 47.23 | 45.0809 |
| fxKcenters | without | 1.003 | 3 | 40.09 → 5.01 → 22.23 | 37.8161 |
| dsFBKc2 | without | 1.002 | 1 | 33.92 | 30.6845 |

171.15 is the highest kcenters test figure recorded, and it is NOT offered as evidence for the
`check` command: node 0 on this task spans 10.58 to 186.33 across seven runs, so one high draw
proves nothing. Two things in the table are worth more than the headline.

**The champion is node 0 again** — the fifth kcenters run of six where the search does not beat its
own first attempt, consistent with section 8's 10-of-18 across all tasks.

**The child is valid.** dsChkKc's node 1 scored 140.40, a worse but LEGAL solver, where dsKcCtl's
node 1 is the 0.0 that started this whole thread — the child that traded exactness for speed,
self-checked 55 times without ever calling `is_solution`, and was refused by the engine after
117.9 s. One run is not a pattern, but it is the outcome the command was built to produce, and
`dsChk49` (same command at the graded n=49) is the replicate.

Run shape: 157 min wall, 137 LLM, 6 evaluation, 46 % of the budget in `plan_step`.

### 17a — and the tool-call sequence shows WHY, which the counts could not

Ordered `run_dev_command` / `write_file` / `edit_file` spans from dsChkKc's node-0 session:

```
+0m   write_file
+0m   check: INVALID   Solution is not optimal. Found value: 132.996, Optimal value: 31.5
+1m   edit_file x2
+1m   check: INVALID   Found value: 44.272
+5m   edit_file x4
+7m   check: INVALID   Found value: 44.272
+9m   edit_file x2
+9m   check: OK
+9m   eval_train                      <- the expensive one, only now
```

and node 1 at +92m repeats the shape: `write_file → check: INVALID → edit_file → check: OK →
eval_train`. The model is using the cheap check as a GATE in front of the graded measurement, which
is what the command was for and what the raw counts in section 16 could not show. Fourteen `check`
calls in the run, four of them catching an invalid state before any evaluation was spent on it.

Compare dsKcCtl, the same task without the command: 55 free-form probes, one of which called
`is_solution`, and the first thing to notice the suboptimality was the engine — 117.9 s of
evaluation to learn it, node scored 0.0, work discarded.

This is mechanism evidence, not metric evidence. dsChkKc's 171.15 remains a single draw from a
distribution that spans 10.58 to 186.33.
