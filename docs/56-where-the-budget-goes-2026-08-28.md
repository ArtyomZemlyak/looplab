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

---

## 18 — AlgoTuner's "$Y remaining" is litellm's price list, not the bill — and the parity broke in ITS disfavour

Verified end-to-end on the one arm-A run that has exactly one attempt, `gpt-5.6-sol` on
edge_expansion (`AlgoTune/logs/edge_expansion_gpt-5.6-sol_20260827_135219.log`, ledger `arm=solA`):

| | messages / calls | spend |
|---|---|---|
| AlgoTuner's own last budget line | 53 messages | **$0.9959** |
| our meter, `cost_basis: upstream` on all 54 rows | 54 calls | **$0.5084** |

Same call set, 1.3 M prompt and 14.8 k completion tokens, and a factor of 1.96 between them. The
cause is in `AlgoTuner/models/lite_llm_model.py:180`: `_extract_cost_from_response` prefers
`response._hidden_params.response_cost` — **litellm's own price table** — over the `usage.cost` the
provider reported and our proxy passes through untouched. Our figure is what OpenRouter charged;
AlgoTuner's is what litellm guessed.

**The consequence is the opposite of a cost advantage.** `spend_limit` is enforced against the
self-report, so this run stopped believing it had spent $1.00 when it had spent $0.51. Doc 55's
headline — "the reference harness took 338.26 on test for $0.51" — is right about the money and
WRONG about the parity it implies: arm A was not more efficient at equal spend, it was given half
the money and still scored 338.26. That makes the gap in loop shape larger, not smaller.

**What is NOT verified here, and my first attempt at it was wrong.** I tried to extend this across
arm A by comparing each `A-<task>.log`'s final budget line against the ledger's `arm=A` rows for
that task, and got a median ratio of 2.87 — an artefact. Arm A was relaunched, so `arm=A` rows for
a task span EVERY attempt while the log is one attempt: edge_expansion is 447 metered calls against
124 logged messages. The sub-agent that found this filtered the meter to the last attempt's start
and reports the deepseek direction as inverted (self-report ~$0.99 against a metered median $1.04,
max $2.41 on pde_heat1d) — that figure is its measurement, not one I have reproduced, and it is
recorded as such.

---

## 19 — The largest lever in the campaign was a missing pip, and eight runs deleted their own answer

`AlgoTune/scripts/evaluate_results.py:266` runs `python -m pip install . --no-deps
--force-reinstall --no-cache-dir` over the candidate directory as soon as a `setup.py` is present.
The arena venv is a `uv venv` — uv installs no pip — so that branch answered `Setup install failed:
... No module named pip`, which the evaluator renders as `no_speedup{reason: compilation_failed}`
and `speedup: 0.0`.

363 occurrences of that error in the spans. **Eight independent runs wrote `.pyx` + `setup.py`,
saw it, and deleted their own extension 0.2–2.4 minutes later.** All 35 `delete_file` calls in the
entire corpus are `.pyx` or `setup.py` — nothing else was ever deleted.

| run | error at | deleted at | lag | champion |
|---|---|---|---|---|
| dsBud3 | 19.9 m | 20.1 m | 0.2 m | 30.70 |
| dsNew | 21.7 m | 22.0 m | 0.3 m | 28.34 |
| dsBud | 34.6 m | 34.9 m | 0.3 m | 48.83 |
| dsFB2 | 16.5 m | 17.9 m | 1.4 m | 31.87 |
| dsKcRep | 39.1 m | 40.1 m | 1.0 m | 47.23 |
| dsNoDR | 25.1 m | 26.4 m | 1.3 m | 27.59 |
| ds3 | six times | six times | 0.7–3.9 m | 156.43 |
| **sol10** | 43.5 m | **did not delete — patched `setup.py` instead** | | **261.11** |

sol10 substituted `build_ext --inplace` for `install` in `sys.argv` and copied the `.so` under a
`.py` name so the arena would carry it. Three and a half minutes later its node 4 scored **253.289**
against a previous best of 23.72. Every one of its nine later nodes rides that extension. That is
an exploitation of the harness defect rather than an honest optimisation, and 259.68/285.58 should
be remembered as such.

The reading in the model's own words, `dsNew`, `plan_step`, span `c47a5640b9e85ae6`: *"The Cython
build failed … No module named pip. … I must remove setup.py and the .pyx … Simplest: delete both
files, keep pure-Python solver."* The observation is correct and the conclusion is wrong — another
model found the way around in four minutes.

Repaired 2026-08-28 09:0x by installing pip ALONE from the ensurepip wheel (`python -m ensurepip`
would have dropped setuptools 65.5.0 over the 84.0.0 in place, underneath four live evaluations).
Pinned in `benchmarks/box-jhub-l40s.sh::_algotune_ensure_pip` with
`tests/test_arena_can_build_an_extension.py` beside it.

**The discontinuity, stated rather than buried.** Every number in docs 55 and 56 above this section
was measured while this branch of the scorer was broken — so `.pyx` was, in practice, only scorable
by the two runs that worked around it. Section 18's njit-vs-pyx table in particular compares a
technique that mostly worked against one that mostly could not be submitted. And the four probes
live at the moment of the repair — dsChk49, dsN3, dsN3b, dsPyx — straddle it: a `setup.py` written
before 09:0x was doomed and one written after will build. Their numbers are not clean either way.

---

## 20 — The pip repair is probe-verified, and the prediction was recorded before the number

Written down before the result, at 08:57: *"if the mechanism is what was measured, the probe should
go past 150 rather than stay at 30–50."*

| probe | before the repair | after | what it shipped | pip errors / deletes |
|---|---|---|---|---|
| **dsPyx** | node 0 = 21.6461 | node 1 = **242.855** | `edge_cut.pyx` + `setup.py` + `solver.py` | 2 errors, last 08:40:05 (pre-repair), 5 deletes |
| **dsN3b** | — | node 0 = **131.8093** | `edgecut.pyx` + `setup.py` | **0 errors, 1 delete** |
| dsN3 (started 06:09, mostly pre-repair) | node 1 = 165.3182 | — | — | 7 errors, last 08:00:59, 6 deletes |
| dsChk49 (kcenters) | 186.3288 | — | numba, no `.pyx` | 0 errors, 0 deletes |

dsPyx is the same run, the same model and the same card on both sides of the repair: before it, its
`.pyx` was refused and it sat at 21.65; after it, the extension survived and the node scored
**242.86** — an 11.2x move on one run, against a ruler whose noise is ~10 %. dsN3b never saw the
error at all and its FIRST node ships an extension.

Against the $1 ceiling this campaign has been reporting for edge_expansion — champions of 27.19 to
48.83 across eleven runs — this is the ceiling itself moving, not a run getting lucky inside it.

Two honest limits. Neither probe is finished, so these are node metrics on train and not a final
test number. And dsPyx straddles the repair, which makes it excellent evidence for the MECHANISM
(the same agent, refused then allowed) and poor evidence for a clean before/after average. `dsFix1`
was launched at 09:16 on the fully repaired stack from scratch — pip present, the njit-vs-pyx
clause in the card, `check --size 4408` — as the clean baseline.

### 20a — dsPyx finished: 228.61 on test, the first $1 run to ship an extension

| | dsPyx | dsBud (previous $1 best) | dsNew |
|---|---|---|---|
| champion | node 1, **242.855** train / **228.6103 test** | node 1, 48.83 / 48.47 | node 0, 28.34 / 27.80 |
| shipped | `champion_solver.py` + **`edge_cut.pyx` + `setup.py`** | pure numpy, 34 lines | pure numpy |
| $ | 1.009 | 1.006 | 1.005 |
| nodes | 2 | 2 | 3 |
| wall / LLM / evaluation | 145 / 129 / 3 min | 98 / 83 / 3 | 144 / 133 / 4 |
| code share | 68 % | 79 % | 54 % |

**4.7x over the previous best at the same budget**, and the shape of the run is otherwise ordinary
— same money, same node count, same three minutes of evaluation. Nothing about the search changed;
what changed is that the extension it wrote was allowed to be scored.

`extract_champion` carried both companions (`+2 more: edge_cut.pyx, setup.py`), so the published
artefact is the whole thing rather than an orphaned import — the defect fixed in 136442be would
have silently turned this run into a 0.0.

Against the benchmark's own published champion for this task: Gemini 3.1 ships 12 lines of
`solver.py` over a 33-line `.pyx`; ours is 95 lines over a 25-line `.pyx`. Same technique, more
Python around it.

The honest caveat repeated: dsPyx STRADDLES the pip repair — its node 0 (21.6461) was refused an
extension and its node 1 was not. It is mechanism evidence. `dsFix1` (09:16) and `dsFix2` (09:32)
are the clean runs from scratch on the repaired stack, and they are what the new $1 baseline should
be read from.

---

## 21 — Corrections after an independent review, each re-verified here

A second sub-agent re-derived the corpus with its own code. It found defects in this document and
in the plot report. Everything below was re-measured by me before being written down.

**21.1 — §8's headline is an OLD-EPOCH statement and does not hold on the repaired stack.**
Re-measured, splitting the probes by epoch:

| slice | n | median gain over node 0 | champion IS node 0 |
|---|---|---|---|
| old card / broken scorer | 16 | **1.00x** | 10/16 |
| fresh (fixed card, most fixes) | 10 | **1.25x** | **3/10** |

So "the search does not beat its own first attempt" is true of the corpus §8 was written from and
false of the current one. §8 stands as history, not as a property of the loop.

**21.2 — the "ceiling effect" is an artifact of dividing by the number it is compared to.**
I reported ρ(LoopLab/AlgoTuner, AlgoTuner) = −0.75 as a clear finding and told the operator it
explains the wins. Permutation null with the two arms shuffled — no relation by construction —
gives mean **−0.526**, 5–95 % **−0.84…−0.14**, and the observed −0.745 sits at the **17th
percentile, p ≈ 0.17**. A ratio with `a` in the denominator correlates negatively with `a` for
random data. The hypothesis verdict (4 wins / 3 ties / 3 losses, |ρ| ≤ 0.24 against every
difficulty proxy) survives; only my EXPLANATION of it is withdrawn.

**21.3 — the span stream loses money, but only on the campaign runs.** Re-measured `events.jsonl`
`llm_usage` against `spans.jsonl` `generation.cost`:

* `runs-B/*` (campaign): 5.5 % to **19.4 %** missing (edge_expansion 379 usage events vs 320 spans).
* `model-probes/*` (every probe): **0.0 % to 0.3 %**.

Every phase table in this document was computed on PROBES, so §1, §9, §13 and §17 stand. What does
not stand is the campaign figure I passed on to the operator — "LoopLab came in under its cap at
$0.90" is really **$1.003**, exactly at the cap. The arm-A side of that comparison ($1.19) holds.

**21.4 — §16's ratio does not survive its own replicates.** Final counters, `check` vs `eval_train`:
dsChkKc 14/11, dsChk49 12/13, dsN3 18/18, dsPyx 20/21, dsN3b 4/6 — pooled **68 vs 66**, a tie, and
`check` exceeds `eval_train` in exactly one run of five, the one §16 measured mid-flight. `eval_train`
did not drop either: 6–13 without the command, 11–21 with it. **§17a survives** — the gate sequence
(`check` INVALID → edits → `check` OK → `eval_train`) is a mechanism claim and is unaffected.

**21.5 — the ~10 % noise band is weakly supported and the 2 % figure is the measured one.**
My 285.5765 re-score of sol10 was a live run whose artefact was never saved; the only persisted
paired re-measurement of ONE solver on ONE subset in the corpus is fxSpectral, 2.1766 → 2.2244,
**+2.20 %**. The other apparent pairs (ctlEdge 26.45→27.74, fxKcenters 20.81→37.82) are DIFFERENT
solvers — last node, then champion — and reading them as ruler noise would be an 81 % error.
Every "below the 10 % noise" dismissal above should be read as provisional.

**21.6 — dsFB's 207.48 was also obtained around the broken scorer.** Its champion imports
`pyximport` (3 mentions) and compiles at import instead of through `setup.py install`. §19 gave
sol10 that caveat and not dsFB; both need it. The review counts eleven runs hitting
`No module named pip` and four distinct workarounds (sol10's argv patch, dsFB's pyximport, dsN3's
inlined source, dsBud2's subprocess build).

**21.7 — a third genuine test-0.0, missing from §4's list.** `B/max_common_subgraph`: train
champion 101.81, test **0.0** at `eval_seconds` 119.2 with `invalid_results` 98/100. Larger than
ds3's gap and not previously recorded.

**21.8 — my diagnosis of ds3's test 0.0 is withdrawn, and not replaced with a guess.**
I published, and repeated, that ds3's train 156.4328 → test 0.0 "reproduces deterministically and
is NOT the .pyx: the solver crashes on a data shape absent from train." The evidence does not
support that. `final.json`'s `stderr_tail` reads `Compilation needed: 1 tasks` / `Failed
evaluations: 1` / `Critical execution error encountered`, i.e. the failure is on the compilation
path, not in a solver branch.

What is now established about the artefact, measured rather than assumed: the node wrote
`cutcounter.pyx` and `solver.py` and **no `setup.py`**; the champion calls `pyximport.install()`
and imports `cutcounter`; `extract_champion` carried both files, so the artefact is complete
relative to what the agent produced. And `AlgoTune/scripts/evaluate_results.py` has branches for
`setup.py`/`pyproject.toml` (pip), Pythran and DaCe — and **no branch for a bare `.pyx`**. So ds3
was compiling with `pyximport` INSIDE the timed evaluation, which is environment-dependent and
evidently succeeded on train and not on test.

That is a mechanism, not a proof. The decisive test is a re-score of the champion now that pip
exists, and it must run on a 22-core lane: attempted on the free 8-core set it was correctly
refused with `baseline_regime_mismatch` — the bridge's own guard, working. Queued for the first
free lane rather than settled by argument.

Until then ds3's 0.0 is an OPEN question, and every place this document leans on it as "a genuine
solver failure on unseen data" (§4, §15) should be read as unsupported.

**21.9 — §4's zero count was scoped to probes; the conclusion survives the wider sweep and gets
stronger.** Re-swept `model-probes/**` AND `runs-B/**`: **19 zero-metric node scores, not 7**. But
`eval_seconds` runs 29.7 s to 329.5 s and **none is under 10 s**, so the finding stands with 19 of
19 rather than 7 of 7 — there is no ruler refusal anywhere among node evaluations. The missing
twelve are all campaign runs (spectral_clustering, pde_heat1d, rbf_interpolation,
sparse_eigenvectors_complex ×3, max_clique_cpsat ×3, rectanglepacking), mostly `invalid_results`.

**21.10 — nine campaign node-0 scores are cold-baseline artefacts and must not be used as a search
baseline.** All nine evaluations over 300 s in the campaign are `node_0`, all carry
`baseline_source: in-harness`, and their speedups cluster at 1.0: max_weighted_independent_set
374.6 s / 1.3467, max_independent_set_cpsat 344.1 / 1.1444, max_clique_cpsat 344.0 / 1.0469,
queens_with_obstacles 332.2 / 1.0247, max_common_subgraph 330.0 / 0.9808, rectanglepacking
329.5 / **0.0**. That is the reference being timed in the same pass, on a cache that was cold when
the campaign opened.

Checked whether it is still live: it is not. Fresh probes evaluate node 0 in **39.2–47.1 s**
(dsFix1 39.2, dsFix2 40.0, dsPyx 39.4, dsN3b 41.7, dsBud 47.1) against a warm 44-entry cache. The
`in-harness` string only says the arena's record exposes no cacheable `baseline_time_ms`; it is not
the same claim as "the reference was timed in this pass".

Consequence for the statistics: any "gain over node 0" computed on the CAMPAIGN slice is inflated,
because a fake ~1.0 node 0 is trivially beaten. §8 and §21.1 were computed on probes only and are
unaffected; the plot report pooled both and its 1.05x should be read with this in mind.

**21.11 — the retry counter is probe-verified, and the rate limiter is not a bottleneck.**
At 10:38 three probes on the 8803 proxy took a `BrokenPipeError` within 31 seconds of each other —
`status=200` on all three, so the upstream had begun answering and the stream was cut mid-flight.
The proxy retried and **no work was lost**: zero `node_failed` and zero `pause` across dsFix1,
dsFix2 and dsN3b.

That incident verified a fix inherited from `10a79c3e` that had never been exercised. The new proxy
recorded `attempts=2, queued_s=60.0` (dsN3b, dsFix1) and `attempts=3, queued_s=120.0` (dsFix2)
against latencies of 66.5 s, 61.8 s and 121.1 s. Before that commit a call retried five times wrote
`attempts: 1, queued_s: 0.0`. The old 8801 ledger still carries 279 rows with `attempts: null`, the
pre-fix shape.

Two things I got wrong on the way and correct here. `queued_s` is NOT the retry backoff — it
accumulates `limiter.acquire()`, the RPM queue — so reading 60.0 as a backoff sum that should have
been `min(2**n, 30)` was my error. And the guess that followed it, "three probes on one 45-rpm
proxy are rate-limit bound", is refuted by measurement: over three hours, **240 s of queueing
against 18,427 s of calls on 8803 (1 %)** and 60 s against 18,717 s on 8801 (0 %), with a median
and p90 of 0.0 s and exactly 3 of 605 calls ever queueing — the same three. Adding proxies or
raising `--rpm` would buy nothing at this concurrency.

---

## 22 — ds3's 0.0 is settled on the third try: the shared venv was shadowing the candidate's build

§21.8 withdrew "a data shape absent from train" and left the question open. The re-score on a
proper 22-core lane, with pip present, still returned **0.0 / `evaluator_error` in 28.7 s** — so the
pip theory was wrong too. `looplab_check.py` then named it in one call:

```
TypeError: count_cut() takes exactly 3 positional arguments (2 given)
```

But `cutcounter.pyx` declares `count_cut(list adj, uint8_t[::1] S)` — **two** arguments, and the
champion calls it with two. They agree. What did not agree was the import: a stale
`cutcounter.cpython-311-x86_64-linux-gnu.so` sitting in
`AlgoTune/.venv/lib/python3.11/site-packages`, installed 2026-08-27 19:56, shadowing the
candidate's own build. Move that one file aside and the same champion is **valid on every
instance**.

**How it got there is the pip repair of `bee6a83b`.** `evaluate_results.py:266` runs
`pip install .` over the candidate directory; with no pip it failed harmlessly, with pip it
succeeds and the extension lands in the SHARED venv and outlives the run. Six were there by
midday — `cutcounter`, `edge_cut`, `edge_flatten`, `edgecut`, `_fast_cut`, `solver_ext` — five
within ninety minutes, and one of them, `_kern`, was mine, installed by the very test that verified
the repair.

Fixed in `d439c966`: the bridge sets `PIP_TARGET` to a per-invocation `mkdtemp` and puts it first
on `PYTHONPATH`, so pip's own redirect isolates the install and the arena is not patched. Verified
end to end on a lane: re-evaluating dsPyx's champion left `site-packages` at **12 `.so` files,
unchanged**, and its `edge_cut` extension landed in `/tmp/looplab-piptarget-75afzws9/` instead.

Three lessons recorded rather than smoothed over. My first two diagnoses of this 0.0 were both
wrong and both were published. The repair that unlocked the biggest win of the campaign opened a
contamination channel within the same morning. And the tool that finally named the cause is the
`check` command built two days earlier for the agent, not for me.

**State of the shared venv:** the stale `cutcounter` was moved aside and my `_kern` deleted. The
four modules belonging to probes that were live at the time were left in place — removing a module
a running evaluation might import is a risk with no matching reward, and the redirect stops any
NEW ones. They should be cleared before the next campaign.

**22.1 — a second paired re-measurement, and why it is not the clean one §21.5 asked for.**
The isolation check re-evaluated dsPyx's champion on a 22-core lane and returned **248.0918**
against the **228.6103** in its own `final.json` — same solver, same test subset, same lane width,
**+8.52 %**. Beside the only other persisted pair (`fxSpectral` 2.1766 → 2.2244, **+2.20 %**), that
is the shape I claimed and then had to soften in §21.5: noise grows with the magnitude being
measured.

It is still not proof, and the confound is in the record rather than in a footnote: the original
evaluation took **43.7 s** and the re-run **178.8 s**, four times longer, because it rebuilt the
extension and ran while three probes were live on the other lanes. A speedup is a ratio of times,
so a differently-loaded box is not a repetition. A clean noise figure needs a quiet machine and a
warm build, and neither of the two pairs on record has both.

So the working band stays what it was: ~2 % measured at low speedup, ~8.5 % suggested at high
speedup under load, and any single-probe difference inside those should be treated as provisional.

---

## 23 — The new $1 baseline, n=2: both clean runs ship an extension and land at 106 and 133

`dsFix1` (09:16) and `dsFix2` (09:32) are the first probes started from scratch on the fully
repaired stack — pip present, install redirected, the njit-vs-pyx clause and the
companions-are-submitted clause in the card, `check` at the graded size.

| | dsFix1 | dsFix2 | old $1 ceiling (11 runs) |
|---|---|---|---|
| champion | node 1 | node 2 | — |
| train | 106.9037 | 136.1786 | 21.33 – 48.83 |
| **test** | **106.4716** | **132.7** | **27.19 – 48.83** |
| shipped | `solver_ext.pyx` + `setup.py` | `cutext.pyx` + `setup.py` | pure Python or numba |
| $ | 1.014 | 1.009 | ~1.01 |
| nodes | 3 | 3 | 2 – 4 |

Both landed **above the entire previous range**, at the same budget and the same node count. The
lowest of the two is 2.2x the best of the eleven. This is the ceiling moving, and unlike dsPyx and
dsN3b — which straddled the repair — these two were clean from the first node.

What did NOT change: node count (3 and 3, inside the old 2–4), spend, and the shape of the run.
The search still spends its money the same way; it is the technique it can now submit that differs.

dsFix1's third node is worth its own line: it scored **0.0** having written nothing at all, and
that turned out to be a regression I introduced this morning — see the commit `156b991e`. The node
cost 36.1 s of evaluation and one slot of three.

Still n=2 against a ruler whose repeat spread is 2–8.5 %, so the SIZE of the gap is not settled;
its direction and its mechanism are.

---

## 24 — `declare_stages` is never called, and its 5,001 characters cost 5 % of every run

Measured across six probes on the current stack: **`declare_stages` was called 0 times**, while
the guidance block that teaches it sits in every `plan` / `plan_step` / `card_build` system prompt.

| probe | generations carrying the block | cost of the block | share of the run |
|---|---|---|---|
| dsFix1 | 167 | $0.0570 of $1.013 | 5.6 % |
| dsFix2 | 143 | $0.0488 of $1.009 | 4.8 % |
| dsPyx | 176 | $0.0601 of $1.009 | 6.0 % |
| dsN3b | 233 | $0.0796 of $1.548 | 5.1 % |

(9,763 rendered characters ≈ 2,440 prompt tokens per generation at the calibrated $0.14/Mtok.)

The block is 5,001 source characters about GPU training, checkpoints, shards, `train.py` and
`inline_repair_retrain_cap`, addressed to a role that on these tasks has one stage — `score` — and
nothing to declare. Roughly a sixth of a node at the measured $0.35/node.

**NOT CHANGED TODAY, and the reason is a contract rather than caution.** `_system_body`'s own
docstring pins that `developer_probe=False` must reproduce the historical prompt BYTE FOR BYTE via
`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, so a resumed pre-2026-08-13 run keeps the prompt its first half
ran under. Removing the block silently breaks every such resume, so the change has to arrive as an
opt-in setting defaulting to today's text. I attempted the mechanical hoist, broke the module's
syntax on the first try, and reverted rather than push a shared prompt edit through at the end of a
long pass. Queued with its measurement attached.

---

## 25 — The prediction I recorded for kcenters is falsified, and the reason was already in §18

Before launching `dsFixKc` — the first clean run on kcenters with the whole repaired stack — I wrote
down the expectation: *"if the mechanism is the same as on edge_expansion, the champion should ship
a `.pyx` and go above 186."*

It did not, and the family says why:

| probe | nodes | `.pyx` written | technique | test |
|---|---|---|---|---|
| **dsFixKc** (fresh, full stack) | 117.1 … | **0** | `@njit` (6 mentions) | running |
| dsChk49 | 186.3 → 163.2 | 0 | `@njit` ×16 | 171.7835 |
| dsChkKc | 185.7 → 140.4 | 0 | `@njit` ×11 | 171.1507 |
| dsFBKc | 174.4 → 124.3 | 0 | `@njit` ×9 | 162.1315 |
| dsKcCtl | 90.8 → 0.0 | 0 | numba | 84.6247 |
| dsKcRep | 10.6 → 47.2 | 0 | — | 45.0809 |
| fxKcenters | 40.1 → 5.0 → 22.2 | 0 | — | 37.8161 |
| dsFBKc2 | 33.9 | 0 | — | 30.6845 |

**Not one kcenters run in the corpus has ever written a `.pyx`**, and the three best are numba. So
the pip defect — decisive on edge_expansion, where a C extension is the only thing that breaks 50 —
is irrelevant here, because the winning technique on this task was never blocked.

That is exactly what §18 measured and I failed to carry forward: `@njit` is worth **3.6x** over
plain numpy on kcenters and **nothing** on edge_expansion, and there is no `.pyx` in the kcenters
corpus at all. I generalised a mechanism across tasks after having already recorded that it does
not generalise.

Consequence for the campaign-level claim: the pip repair raises the ceiling on tasks where a
compiled C extension is the winning technique. It is not a uniform uplift, and any re-measurement
should be read per task rather than pooled.

**23.1 — n=3 qualifies §23: the extension is necessary, not sufficient.**
`dsFix3`, the third clean run, **did** ship `solver_cy.pyx` + `setup.py` — on its FIRST node — and
scored **27.9939**, inside the old pure-Python band. Its node 1 dropped the extension and fell to
8.593.

| clean run | node that shipped a `.pyx` | its score |
|---|---|---|
| dsFix1 | node 1 | 106.9037 |
| dsFix2 | node 2 | 136.1786 |
| **dsFix3** | **node 0** | **27.9939** |

So §23's "both clean runs ship an extension and land at 106 and 133" describes two of three. The
repair removes the wall; what the model builds behind it still decides the number, and a compiled
kernel can be as slow as good numpy. Any claim of a new $1 baseline should be read as 28–136 at
n=3, not as a level.

One thing the third run does support: **no source inlining**. dsFix3 wrote a real `.pyx` file,
where dsN3 embedded ~90 lines of Cython in a `_KERNEL_SRC` string to hedge against companions not
being submitted. That is the behaviour `c52bcb00`'s card clause was added for, at n=1 and with the
run unfinished.

**22.2 — the contamination was still biasing measurements, including mine, and the venv is now
clean.** Comparing two compiled kernels on the same task — dsFix1's node 1 at 106.9037 against
dsFix3's node 0 at 27.9939 — I timed both with `looplab_check.py` and got 0.0003 s against 0.39 s,
a factor of 1,300. That number is **not reportable**: `solver_ext`, dsFix1's module, was installed
in the shared venv (10:46, before `d439c966`), so its import resolved to a compiled binary, while
`solver_cy` was not there. I compared a compiled import against something else. The arithmetic gave
it away before the cause did — at 0.39 s per solve a candidate would be SLOWER than the ~43.9 ms
reference and score below 1.0, not 27.99.

The live risk was worse than the spoiled comparison. Of 27 module names imported by probe solvers,
**four collided with the installed set** — `_fast_cut`, `edge_cut`, `edgecut`, `solver_ext` — and
`dsN3b`, running at the time, imports `_fast_cut` and `edgecut`. A rebuild with a changed signature
would have hit exactly the ds3 failure on a live run.

All five candidate modules (`_fast_cut`, `edge_cut`, `edgecut`, `solver_ext`, `edge_flatten`) are
quarantined out of `site-packages`, which now holds only genuine third-party extensions. Future
evaluations install into their per-invocation `PIP_TARGET`, so nothing replaces them. All four
probes survived; no `node_failed` and no `pause` appeared. One correction to my own procedure: I
labelled the check "no evaluation in flight" when the grep had in fact matched one 18 seconds old.
It survived, and the label was still wrong.

---

## 26 — Three probes closed, and §23.1 is corrected: dsFix3 never had a working extension

| probe | task | $ | nodes | champion | test | shipped |
|---|---|---|---|---|---|---|
| dsFix3 | edge_expansion | 1.004 | 3 | node 0 | **27.7907** | a `.pyx` that does not compile and is never imported |
| dsFixKc | kcenters | 1.004 | 2 | node 1 | **159.5116** | numba, no `.pyx` |
| dsN3 | edge_expansion ($3) | 3.002 | 6 | node 4 | **190.0012** | no `.pyx` on disk (Cython source inlined in a string) |

**§23.1 was wrong and is withdrawn.** I recorded dsFix3 as a clean run that "shipped an extension
and scored 27.99", which made the extension look necessary-but-not-sufficient. It is worse and
simpler than that: `solver_cy.pyx` fails to cythonize (`18:13: cdef statement not allowed here`)
and the node's 56-line `solver.py` **never imports it** — the 27.79 is a numba score, and the
`.pyx` is a dead file. So the working-extension sample on the repaired stack is **two**, dsFix1
106.4716 and dsFix2 132.7, not three, and the old $1 ceiling of 27.19–48.83 still stands unbroken
by any run that did not get a compiled kernel to load.

`dsFixKc` closes the kcenters question at 159.5116 with no `.pyx` in two nodes, confirming §25 on
finished numbers: the technique there is numba and the pip repair is irrelevant to it.

`dsN3` at $3 reached six nodes and 190.0012, its champion being node 4 — the search DOES pay at
that node count, which is §21.1's fresh-epoch reading holding on a longer run.

## 27 — Only twenty tasks have data, and a probe on a twenty-first cost $0.0042

I picked `count_connected_components` as a second extension-rewarding task because it has five
published `.pyx` champions, launched it, and killed it a minute later: the arena keeps exactly the
twenty campaign tasks on disk and `make_task.py` will build a card for any name. Nothing refused
it — lane free, fence closed, directory clean.

Of the tasks that DO have data, `integer_factorization` is the one with published `.pyx`
champions (three), and it carries both arms' numbers (AlgoTuner 9.763, LoopLab 9.147). Two probes
are on it now. `run_probe.sh` gained a dataset guard in `cab2692c`.

**21.12 — the stale meter is finally retired.** The 8801 proxy had run since 2026-08-24 10:11:59,
five commits behind its own file, and could not be restarted while probes were mid-flight through
it (`3e848107`). At 16:27 its last call was 47 minutes old — dsN3, finished — and every live probe
was on 8803, so it was stopped by PID and restarted on the current code. Smoke-tested at HTTP 200;
all four running probes unaffected. The staleness guard added in `2372d63a`, which had printed a
warning on every sweep since morning, now reports **"all processes newer than their code"**.

Two consequences worth stating. Every number metered from here on is computed by the code in the
tree — the retry counter, the aborted-stream classification and the synthetic usage frame all
match what the repository says they do. And the failure counts I reported all day from 8801 remain
upper bounds, because that process still classified an aborted stream as an error.

---

## 28 — The ceiling moves on a SECOND task, which is what §25 said still had to be shown

`dsIF` was launched on `integer_factorization` for one reason: of the twenty tasks with data on
disk, it is the only one that has published `.pyx` champions (three) AND numbers from both campaign
arms. §25 had just shown the pip repair is irrelevant on kcenters, where nobody compiles, so the
open question was whether `edge_expansion` was special.

| | AlgoTuner (arm A) | LoopLab campaign (arm B) | **dsIF, node 0** |
|---|---|---|---|
| integer_factorization | 9.763 | 9.147 | **97.6644** |

Its first node ships `factor.pyx` + `setup.py`, the solver imports it (10 references), and the
build passes — checked by compiling it here, not inferred from the score. Ten times the number
either arm reached in the campaign, on the first node of a $1 run.

Beside it, `dsFix4` node 0 on edge_expansion is **177.8392** with `edge_cut.pyx` + `setup.py`,
also compiling — the highest FIRST node that task has produced at $1, against a previous node-0
range of 20.36–37.55.

So the effect is not a property of one task. What §25 established still holds and is the other half
of the statement: it is a property of tasks where a compiled C extension is the winning technique,
and kcenters — where every run of eight reaches its ceiling with numba and none has ever written a
`.pyx` — shows nothing.

Both probes are unfinished and these are train numbers on single nodes. What is settled is the
mechanism on a second task; the size is not.

**28.1 — replicated on the second task: n=2, both from a working extension on node 0.**

| integer_factorization | AlgoTuner (arm A) | LoopLab campaign (arm B) | dsIF node 0 | dsIF2 node 0 |
|---|---|---|---|---|
| test / train | 9.763 | 9.147 | **97.6644** | **180.1625** |
| shipped | — | — | `factor.pyx` (10 imports) | `rho64.pyx` (6 imports) |
| build verified here | — | — | compiles | compiles |

Both extensions were compiled by hand on a lane before either number was written down, so neither
is a dsFix3-style dead file. Ten to twenty times what either arm reached in the campaign, on the
FIRST node of a $1 run, twice independently.

Together with dsFix4's 177.8392 on edge_expansion — also node 0, also a compiling `edge_cut.pyx`
— the pattern across the two extension-rewarding tasks is now four first-nodes in a row above
anything the old stack produced on them.

Limits unchanged and repeated because they matter: these are train numbers on single nodes of
unfinished runs, and §25 still bounds the claim to tasks where a compiled extension is the winning
technique. The task set with data on disk is twenty, and `integer_factorization` was the only
untried member of that class — further evidence has to come from replicates, not from new tasks.

## 29 — dsFix4 closes the edge_expansion baseline at n=3 working extensions

| clean run | nodes | champion | test | shipped |
|---|---|---|---|---|
| dsFix1 | 23.4 → 106.9 → 0.0 | node 1 | 106.4716 | `solver_ext.pyx` |
| dsFix2 | 25.0 → 27.2 → 136.2 | node 2 | 132.7 | `cutext.pyx` |
| **dsFix4** | **177.8 → 149.6 → 206.1** | node 2 | **202.7654** | `edge_cut.pyx` |
| dsFix3 | 28.0 → 8.6 → 27.8 | node 0 | 27.7907 | a `.pyx` that never compiled |

Three runs with a working extension: **106.47, 132.70, 202.77** on test, against the pre-repair
$1 ceiling of 27.19–48.83 across eleven runs. The one that failed to compile landed at 27.79,
inside the old band — which is the cleanest statement of the mechanism this campaign has: the
number tracks whether a compiled kernel loads, not whether the model tried.

dsFix4 is also the first run to start high — its node 0 is 177.8, where dsFix1 and dsFix2 opened
at 23.4 and 25.0 and needed a second or third node to get there. It finished in **108 minutes**
against 155 and 205, at the same $1.01 and the same 3 nodes, spending 51 % on code. Nothing about
the loop changed between them; the first draft happened to reach for the extension immediately.

Against the benchmark's published champion for the task: Gemini 3.1 ships 12 lines of `solver.py`
over a 33-line `.pyx`; ours is 39 over 45. Same technique, more Python around it — unchanged from
what §20a recorded for dsPyx.

## 30 — dsN3b at $3: 247.65 on test, and the budget buys nodes that the extension then uses

| run | budget | nodes | champion | test | shipped |
|---|---|---|---|---|---|
| dsFix1/2/4 | $1 | 3 each | node 1–2 | 106.47 / 132.70 / 202.77 | working `.pyx` |
| dsN3 | $3 | 6 | node 4 | 190.0012 | Cython inlined in a string |
| **dsN3b** | **$3** | **5** | **node 1** | **247.6525** | `_fast_cut.pyx` + `setup.py` |

dsN3b is the highest test figure this campaign has produced on edge_expansion. It is not a
different mechanism from the $1 runs — the same compiled kernel, shipped as real files — but it
reached it on node 1 of five rather than being cut off at three.

Read against §21.1, which found the search beats its own node 0 on the fresh stack (median 1.25x,
champion is node 0 in only 3 of 10) where the old corpus did not (1.00x, 10 of 16): the two $3 runs
put their champions at nodes 1 and 4, so the extra nodes are used rather than merely counted.

What is NOT claimed: that $3 is worth 3x. dsN3 spent $3.002 for 190.00 and dsFix4 spent $1.008 for
202.77 — more budget did not beat the best dollar run on this task. The honest statement is that
budget buys node count, node count is where the search pays on the repaired stack, and the two are
not the same thing as a better number.

---

## 31 — Two ruler worries, both measured and both empty

Neither of these was a report of a defect; both are the kind of thing that quietly invalidates a
campaign if nobody checks, so they are recorded with their numbers rather than left as assumptions.

**The agent's own probes do not run during a scored evaluation.** `run_probe` launches
`probe_launcher.py` as a direct child of the run, inheriting the LANE's core affinity — so a probe
running while a node is being timed would steal cores from the measurement that scores it. Checked
by overlapping span windows across six runs: **0 of 32 node evaluations overlapped any `run_probe`
or `run_dev_command` call**. The loop serialises them; the writing session ends before the
evaluation starts.

**Concurrent lanes do not perturb each other.** Four probes share one box on disjoint 22-core
lanes, but not disjoint memory bandwidth. Over 99 `edge_expansion` evaluations, `eval_seconds` has
a median of **40.5 s when isolated** and **40.1 s with two or more evaluations nearby** — no
measurable coupling. (Density is approximated by `score.log` mtimes within a ±15-minute window,
which is a proxy for concurrency rather than a record of it; the point is the absence of a gap, and
a real effect large enough to matter would not hide behind that approximation.)

Taken with §21.10 — the 300 s cold-baseline node-0 evaluations that were an artefact of a cold
cache and are gone on the warm one — the timing side of the ruler has now been checked from three
directions and has held each time.

## 32 — integer_factorization closed at n=2, and a display trap of my own

| | AlgoTuner | LoopLab campaign | dsIF | dsIF2 |
|---|---|---|---|---|
| test | 9.763 | 9.147 | **96.2102** | **178.7585** |
| champion | — | — | node 0 | node 0 |
| shipped | — | — | `factor.pyx` (198 lines) | `rho64.pyx` |
| $ / nodes / wall | — | — | 1.009 / 2 / 215 min | 1.012 / 3 / 149 min |

Ten to nineteen times either campaign arm, both from a compiled kernel on the first node, both
builds verified by hand before the numbers were written down.

Our kernel is the largest of the four on this task: 198 lines against **91** (Gemini 3.1 Pro),
**146** (Gemini 3 Pro) and **84** (GPT-5.4) in `.foreign_results_held`. Same technique, twice the
code — the same shape §20a found on edge_expansion.

**A trap in my own reporting, not in the ruler.** Rounding node metrics to one decimal printed
dsIF node 1 as `0.0` and dsIF2 node 2 as `1.0`, which read as a zero and a no-op. They are
**0.0137** and **0.9702**: valid solvers with no refusal reason, just slow. dsIF's node 1 took
238 s to evaluate and is ~70x SLOWER than the reference and ~7,000x worse than its own parent —
a real regression inside a run, and worth noticing precisely because it is not a zero.

## 33. The number tracks whether the kernel LOADS — and two ways it fails to

Thirty-nine nodes across the probe corpus shipped a `.pyx`. Splitting them by what the bridge's
build actually did:

| what happened to the kernel | n | median speedup | values |
|---|---|---|---|
| `build_ext ok` | 34 | **192.32** | 261.11 … 16.60 |
| compile failed (`rc=1`, Cython diagnostics) | 2 | **25.41** | 27.99, 22.82 |
| `.pyx` shipped with **no** `setup.py` | 3 | **34.65** | 156.43, 34.65, 24.91 |

The middle row is the sharpest statement this corpus supports about the pip repair: a kernel that
compiles is worth roughly **7.6×** the median of one that does not. It is not that Cython is magic —
it is that a `.pyx` which fails to load leaves the run graded on whatever pure-Python path remained,
which is where the old 22–48 band lived. dsFix3's 27.79 was never a bad idea badly executed; it was
a good idea that did not link.

The third row is a distinct failure the loop had no words for, and it was **ours, not the model's**.
The bridge's predicate fired on the presence of a `.pyx` alone, so a submission with a Cython source
and no recipe ran `python setup.py build_ext --inplace` in a directory containing no such file and
reported

    build_ext failed rc=2: .../python: can't open file '.../setup.py': [Errno 2] No such file

— a complaint about a file the model never wrote. Three runs (ds3 node_0 and node_6, dsFB node_2)
carried a dead Cython source through grading and were never told. Fixed in `3be289eb`:
`build_decision()` now names the source, names the missing recipe, and states the consequence.
ds3 node_0's 156.43 shows the failure is survivable when `solver.py` never imported the extension —
which is exactly why it went unnoticed for eleven runs.

### 33.1 The syntax gate the reference agent has, and why we do not need it

`AlgoTuner/editor/editor_functions.py` validates every edit with `ast.parse` plus pylint and reverts
the write when it fails, so a broken file never reaches disk. The obvious move is to port it. It was
measured first, over all 188 `.py` files written across the probe corpus:

**zero** fail to parse.

The gate would fire on nothing. Its cost is not zero either — the reference agent's version also runs
pylint and refuses on *lint* errors, which turns a style opinion into a blocked edit. This is
recorded as a **negative** result so the idea is not re-derived: the reference agent needs the gate
because its edits are line-range splices into an existing file, where an off-by-one truncates a
block; ours are whole-file writes, which are wrong or right but rarely unparseable.

## 34. dsIF3 closes integer_factorization at n=3, and the ledger was lying to the two phases that rank

**dsIF3** — `integer_factorization`, $1.004, three nodes, 174 min wall.

| node | speedup (train) | eval s | submission |
|---|---|---|---|
| node_0 | 67.645 | 266.7 | `rho.pyx` + `setup.py` |
| **node_1** | **153.297** | 29.6 | **`factor_cy.pyx`** + `setup.py` |
| node_2 | 147.441 | 29.3 | `rho.pyx` + `setup.py` |

Champion is node_1, confirmed against the extracted `factor_cy.pyx` rather than file times.
**Test: 194.8199.** With dsIF (96.2102) and dsIF2 (178.7585) that is n=3 on the repaired stack
against arm A's 9.763 and arm B's 9.147 — the task the plot report scored as a 0.94× *loss* is a
**20× win** once the extension can compile. All three finals carry the same
`baseline_source: in-harness`, and all three evaluated in 29–34 s with no ~210 s reference pass, so
they are measured the same way as each other and as the campaign.

Where the money and the time went, all three probes:

| probe | wall | LLM | eval | top phases by time |
|---|---|---|---|---|
| dsIF | 216 min | 171 min (80 %, 264 calls) | 4.5 min | plan_step 81, deep_research 27, plan 27 |
| dsIF2 | 150 min | 125 min (84 %, 290 calls) | 2.4 min | plan_step 56, deep_research 24, plan 17 |
| dsIF3 | 174 min | 130 min (75 %, 282 calls) | 5.4 min | plan_step 48, propose 34, plan 21 |

dsIF3's money splits `plan_step` $0.5516 (55 %), `propose` $0.1971, `plan` $0.1108,
`deep_research` $0.0894, everything else under 3 % each. Local evaluation is **2–4 %** of wall clock
on this task. Whatever is expensive here, it is not the ruler.

### 34.1 The defect this probe exposed: `sort="best"` reports an unscored ledger as an empty one

Reading dsIF3's phase spans, `hyp_prioritize` and `foresight_rank` were answered
`(no matching experiments)` while `novelty`, in the same run, was shown `1 of 1 experiment(s)`.
`list_experiments` empty-answer rate across the whole probe corpus, by the sort the caller passed:

| phase | calls | empty | |
|---|---|---|---|
| `foresight_rank` (`sort=best`) | 126 | 47 | **37 %** |
| `hyp_prioritize` (`sort=best`) | 155 | 43 | **28 %** |
| `propose` | 22 | 3 | 14 % |
| `novelty` (`sort=recent`) | 63 | 2 | 3 % |
| `deep_research` (`sort=recent`) | 182 | 2 | 1 % |

`best`/`worst` rank by metric, so `digest.top_nodes` keeps only nodes that are feasible **and
already evaluated**. A run whose experiments are all still drafts therefore hears that its ledger is
empty. **48 of those answers**, across eight runs, sit within five calls of a `sort=recent` answer in
the same run that listed the drafts — same ledger, same moment, opposite story. And `best` is the
DEFAULT, so it is what a caller that passes no `sort` receives.

The two phases this misinforms are exactly the two whose job is to avoid re-proposing work already in
flight, which is the likeliest mechanism behind the duplicate-proposal question left open in §4.
Fixed in `7c5795af`: the answer now names the count and the way to see them, and only when the
metric filter is what emptied the list — a `theme=` matching nothing keeps its own honest zero.

### 34.2 Two guards were left red by my own commit

`3dabc64d` (the stage-guidance switch) added one line to `agents/factory.py` (521 → 522, over the
god-module ratchet) and one field to `Settings` (217 → 218, moving the calibration profile digest).
Both guards were red on HEAD from 19:10 and were found on the next sweep, not at commit time —
because I ran a narrow `-k` selection instead of the suite, for the second time that day. Re-pinned
in `7c5795af` and `5d22360f`, each with the reason and the lateness recorded beside the pin.

## 35. The stage-guidance switch at n=2, and a test that wrote into the ruler

### 35.1 Removing the stage block costs nothing measurable — and does not obviously buy anything

`edge_expansion`, deepseek-v4-flash, $1, same card, same lane width, `developer_stage_guidance`
confirmed in each run's own `config.snapshot.json`:

| stage block | n | runs | median | range |
|---|---|---|---|---|
| ON | 3 | dsFix1 106.47, dsFix2 132.70, dsFix4 202.77 | 132.70 | 106.47–202.77 |
| OFF | 2 | dsNoStg **237.12**, dsNoStg2 **153.40** | 195.26 | 153.40–237.12 |

The OFF range sits inside the ON range's own spread. **This does not decide anything**, and the
tempting read — "removing 5,001 characters made it better" — is not supported at n=2 against a
control band that already spans a factor of 1.9. What it does support is the weaker claim the switch
was built to test: nothing was lost with the block. dsNoStg3 is running for a third point.

Both OFF runs reached their number the same way as the controls: a `.pyx` plus `setup.py` that
compiled. dsNoStg went 27.26 → 237.31 across two nodes; dsNoStg2 went 152.94 → 153.40.

### 35.2 The test that proved the arena can build was building INTO the arena

`test_a_setup_py_candidate_actually_installs` ran `evaluate_results.py`'s own argv with no
`PIP_TARGET`, so at **21:20:01 today** it installed `_kern.cpython-311-x86_64-linux-gnu.so` and
`kern-0.0.0.dist-info` into the arena's shared `site-packages` — **while two probe evaluations were
running against that venv**. This is the contamination channel `d439c966` closed in the bridge, left
open in the test that verifies the bridge's premise.

Nothing was mismeasured. No probe imports the module name `_kern`, checked by module form across the
corpus. My first check used a substring and matched `edge_expansion_kernel` and `_kernel_solve` in
dsBud2 and dsFB2; I nearly reported a collision that does not exist. The defect is the write into a
live ruler, which stands either way. Fixed in `53e69d26` with the same `PIP_TARGET` isolation the
bridge uses plus an assertion that site-packages is unchanged.

The deeper miss is that **nothing was watching**. `check_leaks.sh` had six sections and none looked
at the venv, so this was found by an ad-hoc sweep. Section 7 added in `cfabb1ad`, anchored on the
CAMPAIGN START rather than on `bin/python` — the first anchor reported 173 false positives because
the binary predates the packages the venv was built with. Proven by canary.

### 35.3 Three of my own commits left guards red

| commit | guard | found |
|---|---|---|
| 3dabc64d | `factory.py` god-module ratchet 521 → 522 | one sweep late |
| 3dabc64d | calibration profile digest + field count 217 → 218 | one sweep late |
| 156b991e | `core/models.py:943` line citation in three files | two sweeps late |
| 156b991e | `test_empty_build_is_stuck_not_a_crash.py` re-derived the package walk | two sweeps late |

Every one of them was invisible to the narrow `-k` selections I kept running and visible immediately
to the full suite. The lesson is not "run more tests"; it is that a `-k` filter chosen from the names
of the files I edited cannot see a guard that watches the WHOLE package for a property I just broke.

## 36. Three reference-agent advantages measured, none worth porting today

The sweep's item-8 answer, with the numbers that make it an answer rather than a shrug.

**The validator's line-level failure context.** `AlgoTuner/utils/evaluator/failure_analyzer.py`
traces `is_solution` with `sys.settrace` to find the exact line that returned False, and arm A's
agent is shown up to three of them (`message_writer.py:726-750`). That is genuinely richer than
anything we hand back. It is also unreachable through the script we call — `main.py:1160` attaches
`invalid_solution_analysis` only when a `baseline_manager` is passed and `evaluate_results.py` does
not pass one — which was established earlier and is recorded in the bridge's own header. What is new
here is the size of the prize: across **151 score records in the probe corpus, zero** have
`validity_pct < 100`. The feature would have fired **no times**. Not worth a re-plumbing of the
evaluator call today.

**The `0.0` that is not a measurement.** Eleven records carry `speedup: 0.0` with a live
`eval_seconds`, which reads as a defect and is not one: `_emit` enforces that a non-positive speedup
never ships without a `no_speedup.reason`, deliberately, because `null` would leave the node without
a metric and `metric_salvage` DISCARDS those. The four reasons observed are `no_valid_speedups` (5),
`evaluator_error` (4), `compilation_failed` (1), `solver_unloadable` (1). Checked that the reason
actually REACHES the model by resolving each run's span-input chain: **11 of 11**. The twelfth
apparent case was my own substring match catching `0.0614`.

**"Avoid Cython" in the model's own reasoning.** 18 instances of "avoid cython" and 15 of "skip
cython" across the corpus looked like the loop talking itself out of the move that wins here — the
kernel-vs-no-kernel medians are 192.32 against 25.41 (§33). Measured at the OUTCOME instead of the
sentence: of the 14 probes where the phrase appears, **11 shipped a `.pyx` anyway**, median best node
**180.16**; the 3 that did not sit at median 47.23. The self-talk is deliberation the loop overrules,
not a decision it makes. One `repropose` transcript does show our own "FULLY DIFFERENT … not a
rewording" instruction being read as forbidding a Cython re-implementation of the same algorithm —
worth watching, but it did not change what that run shipped.

**What was fixed instead**, found while checking the fence: the TRACKED `check_leaks.sh` had no
fence check at all. Two scripts of that name had diverged — the tracked one covered more campaign
directories, the operator's ad-hoc copy held the foreign-champion check — and each sweep got
whichever half it invoked. `09ef7171` gives the checkout both, and makes a readable champion set
`BAD=1` rather than a note.

## 37. A third of the budget is spent before the first node, and one run spent all of it

Cost of the run up to the moment the FIRST node is created, across all 48 probes:

| | $ before node 1 | note |
|---|---|---|
| median | **~$0.30** | on a $1.00 budget — about a third |
| worst that still produced nodes | dsFBKc2 $0.833 (2 nodes), fxSpectral $0.703 (3), ds3Hull $0.644 (6) | |
| `convex_hull` band | ds3Hull 0.644, solHull 0.464, dsFBHull 0.405, dsHull 0.385 | the live dsCH at $0.62 with no node yet is INSIDE this band, not stalled |
| best | glm53f $0.028 (21 nodes), gpt56luna $0.048 (22), ctlEdge $0.124 (8) | |
| **produced nothing at all** | **opus5 $1.057** | |

### 37.1 opus5: ten generations, one dollar, zero nodes

Every cent went to a single stage. Phase table for the whole run:

| phase | $ | calls |
|---|---|---|
| `deep_research` | **$1.0204** | 10 |
| everything else | $0 | 0 |

Events: `setup_started`, 2 × `setup_step`, `setup_finished`, `research_attempted`, 11 × `llm_usage`,
`report_generated`, 6 × `finalize_step`, `run_finished`. The last one reads
`{'step': 'abandoned', 'outcome': 'error_terminal'}`. No node was ever created, so there is no
champion to compare with arm A, arm B or the held foreign champions — the comparison this section
would normally make does not exist, and that is the finding.

The per-generation price is what a turn cap cannot see: **$0.102 here against ~$0.003 for
deepseek-v4-flash**, a factor of 34. Ten calls is a modest research budget for one model and the
entire run for another.

### 37.2 The stage that can spend everything is the stage that cannot see the budget

Resolving each run's span-input chain and asking which generations carry a `BUDGET:` line:

| stage | with budget | without | share |
|---|---|---|---|
| `strategist_consult` | 84 | 0 | 100 % |
| `plan` | 1561 | 152 | 91 % |
| `propose` | 2996 | 517 | 85 % |
| `plan_step` | 5112 | 957 | 84 % |
| `repropose` | 985 | 222 | 82 % |
| `card_build` | 180 | 67 | 73 % |
| **`deep_research`** | **2** | **2547** | **0 %** |
| `novelty`, `foresight_rank`, `hyp_prioritize`, `report`, … | 0 | 2233 | 0 % |

`deep_research` is also the only stage with neither a turn cap nor a money cap of its own —
`agent_max_turns` and `agent_time_budget_s` both default to `0`, unlimited. The intersection of
"can spend everything" and "is told nothing about the budget" is exactly one stage, and exactly one
run died in it. Fixed in `a78d295f`; the note leads the user turn and states the consequence rather
than only the number. **Not yet verified by probe** — all four lanes are busy; the next free one
gets a repeat under the changed prompt.

The other 0 % rows are not the same defect: `novelty`, `foresight_rank` and `hyp_prioritize` are
bounded single-shot rankers, not open-ended tool loops, and their corpus cost is 0.4 %, 2.1 % and
2.6 % of a run.

## 38. The stage-guidance switch at n=3 vs n=3: p = 0.10, median ×1.79

dsNoStg3 finished at $1.005 with three nodes — node_0 22.35 (plain Python), node_1 155.94
(`edge_cut.pyx` + `setup.py`), node_2 **271.4507**, the highest single node on `edge_expansion` in
the whole corpus. **Test: 268.0908.** Champion is node_2, matching the extracted file.

| stage block | n | finals | median |
|---|---|---|---|
| ON | 3 | 106.47, 132.70, 202.77 | 132.70 |
| OFF | 3 | 153.40, 237.12, **268.09** | **237.12** |

Sorted, the six interleave as ON, ON, OFF, ON, OFF, OFF. The OFF rank sum is 14 of a possible 15,
and an exact one-sided rank test over all C(6,3) = 20 assignments gives **p = 2/20 = 0.100**. The
median moves by a factor of **1.79**.

That is the strongest evidence this switch has, and it is still not significance at any conventional
threshold. What it is: three runs without the block, and all three beat the median of three runs
with it. Two more runs — one per arm — would settle it, and the honest statement until then is
"suggestive at p = 0.10", not "removing the block helps".

Worth noting against my own §35.1: at n=2 I wrote that the ranges overlapped and decided nothing.
They still overlap (202.77 beats 153.40). The third point did not remove the overlap; it moved the
rank sum.

### 38.1 The profiler the reference agent has, and why it is not being built today

`AlgoTuner/interfaces/commands/types.py` exposes ten commands, of which `profile` and `profile_lines`
have no named counterpart on our side. Our surface is far larger — **54 distinct tools** appear in
the corpus, led by `repo_read` (3363), `read_file` (3232), `read_experiment` (2815), `run_probe`
(2157) — and `run_probe` is the escape hatch through which the model profiles by hand: **189
`time.perf_counter` uses across 28 probes**, plus 14 `timeit` and 2 `cProfile`. So the capability is
there and the convenience is not.

Before building it, the question was measured: does hand-rolled profiling correlate with a better
result? Restricted to $1 `edge_expansion` runs so the comparison is like-for-like:

| | n | median best node | range |
|---|---|---|---|
| profiled at least once | 15 | 48.83 | 27.6–271.5 |
| never profiled | 6 | 27.83 | 19.0–204.5 |

The medians differ by 1.75× and the ranges overlap almost completely — `dsFB` reached 204.47 with
zero profiling, `dsFB2` reached 31.87 with one. At n=15 against n=6 with that spread this is **not
a signal**, and a named profiler is a prompt-surface change that every run would pay for. Not built
today; recorded so the option keeps its evidence.

## 39. The probe convicted my own fix, and it was the right probe to run

`a78d295f` gave `deep_research` a budget line and shipped marked NOT YET VERIFIED BY PROBE. dsBN was
launched to verify it. The line lands — **7 of 7** `deep_research` generations carry it, against 2 of
2,549 across the old corpus. Then reading the actual numbers:

| generation | what the model was told |
|---|---|
| 1–7 | `$0.0000 of $1.0000 spent, $1.0000 left (0 % gone)` |
| 8–11 | `$0.3210 of $1.0000 spent` |

The figure is **constant within a session** and moves only between sessions, because the note was
built into `messages` once and replayed every turn. `plan_step` in the same run shows `$0.0935`
eight times running, so this is not a `deep_research` quirk — it is how every stage's budget line has
always worked.

That makes the shipped fix miss the case it was built for. `opus5` spent its entire **$1.0204 inside
ONE research session** (`research_attempted: 1`, ten generations), so a session-start figure would
have read `$0.0000` ten times and warned nobody. The fix closed the visibility gap and not the
failure.

Repaired in `453c83d9`: `drive_tool_loop` takes an optional `budget_note` CALLABLE, rendered fresh
each turn at the same site the plan reminder already uses, injected as a `user` reminder only when
the rendered text CHANGES. `deep_research` is the only opt-in — it is the only stage with neither a
turn cap nor a money cap. Default `None` keeps every other caller's message list byte-identical.

**The lesson is about the rule, not the bug.** "Verify a card or loop fix BY PROBE, not by
reasoning" is what caught this. The fix was well-argued, tested, mutation-checked three ways, and
wrong about the thing that mattered; no amount of further reasoning about it would have shown the
seven identical `$0.0000` lines.

### 39.1 My "zero failures" reports were sound, and the missing summary was mine

`pyproject.toml` already sets `addopts = "-q"`, and I was passing `-q` again — `-qq` suppresses the
`N passed` line entirely, which is why four sweeps of suite results carried no count. Checked with a
deliberately failing canary rather than assuming: under `-qq` pytest still prints the `FAILED` line
and still exits 1. So the green claims rested on a signal that works; only the positive count was
missing. Dropping my own `-q` restores it.

## 40. dsCH: our worst loss narrows from 0.25× to 0.60×, and stops at one node

`convex_hull`, $1.004, **one node**, test **2.5955** (node_0 2.6757, `solver.py` only — no kernel).

| | speedup | vs dsCH |
|---|---|---|
| arm A (AlgoTuner) | 4.321 | we are at 0.60× |
| arm B (campaign) | 1.089 | we are at 2.38× |
| **dsCH** | **2.5955** | |

So the repaired stack more than doubles the campaign's number on our worst-losing task and still
does not close the gap. The mechanism is visible in one line of the phase table: `plan_step` $0.5619
over 117 calls, `propose` $0.2394, `deep_research` $0.0994 — and **$0.62 was gone before the first
node existed** (§37's table puts the `convex_hull` band at $0.385–0.644, so this is normal for the
task, not a stall). At 72.8 s per evaluation and 267,021 instances, a $1 budget buys exactly one
node here.

What the winners do differently — the seventeen held foreign champions for this task:

| import | champions using it |
|---|---|
| `numba` | **9 of 17** (Opus 4.5, Opus 4.6, GLM-4.5, GPT-5.2, GPT-5.4, Gemini 2.5 Pro, Gemini 3.1 Pro, R1, …) |
| `scipy.spatial` only | 8 of 17 |

Lengths run from 6 lines (Opus 4) to 263 (Opus 4.6). Ours is 110 lines of `numpy` + `scipy.spatial`
— the reference approach with tweaks, which is what a FIRST node looks like. We never reached the
node where the JIT goes in.

**dsCH3 launched on the freed lane**: same task, same model, **$3.00**. It tests exactly the reading
above — that $1 on `convex_hull` buys one node and the loss is a budget-shape problem rather than a
capability one. It compares against dsCH 2.5955, arm A 4.321, arm B 1.089, and against the two other
$3 runs in the corpus that reached 6 and 13 nodes on cheaper tasks.

### 40.1 Two broken pipes, and the money still reconciles

`check_money.sh` flagged `BrokenPipeError` on dsBN 23:42:05 and dsDL 23:42:36. Both carry
**`status=200`**: the upstream call SUCCEEDED and the pipe broke while the response was written
back, i.e. the client went away — the same shape as the 15:59 one, which was my own kill of dsCCC.
Both runs continued normally 30–120 s later and no other arm appears in the window, so the test suite
running at the time never touched the proxy.

The question that mattered is whether we were billed for a response nobody received. Reconciling the
proxy ledger against each run's own `llm_usage` events:

| probe | ledger $ | run $ | difference |
|---|---|---|---|
| dsBN | 0.6059 | 0.6059 | **+0.0000** |
| dsDL | 0.4741 | 0.4741 | **+0.0000** |
| dsIF3x | 2.0808 | 2.0808 | **+0.0000** |
| dsCH | 1.0041 | 1.0041 | **+0.0000** |

Exact to the cent on all four. The ledger holds 1–3 more ENTRIES than the run counts calls, and
those entries carry zero cost.

### 40.2 dsDL node 0 = 0.0: one timeout, then an early exit that fails the rest

A zero with a live `eval_seconds` of **504.0** — seven times the usual 30–40 s. The reason block is
`evaluator_error`, and the harness's own words are the diagnosis:

    [isolated_benchmark] Run 1/3 timed out after 120.0s
    [isolated_benchmark] Early exit enabled - treating all runs as timeout
    Aborting evaluation after 5 consecutive failures

So ONE real timeout at the campaign's `ALGOTUNE_MIN_TIMEOUT_S=120` ceiling cascades: AlgoTune's
early-exit treats every remaining run as a timeout without running it, five consecutive failures
abort the evaluation, and the node scores 0.0. `build_ext ok` is in the same log, so the extension
did compile — the timeout is the solver's, not the build's.

This is the same shape as the 56 unexplained aborts left open in docs/53 §9, on a task not in that
list (`discrete_log`, graded n=25). Recorded here with the harness lines that name the mechanism,
which §9 did not have.

## 41. What the reference agent does about money, and how close we now are

`campaign-final/A-*.log` carries AlgoTuner's system prompt verbatim. Its second sentence is about
cost, not about algorithms:

> Every message you send incurs a cost—you will be informed of your usage and remaining budget by
> the system.

And it keeps that promise on **every** message. Counted across all arm-A logs: **1189 budget lines
against 1184 messages**, live and per-message —

    You have sent 1 messages and have used up $0.0007. You have $0.9993 remaining.
    …
    You have sent 124 messages and have used up $0.9934. You have $0.0066 remaining.

That is the shape `453c83d9` converged on independently, and finding it here after the fact is the
strongest argument for it: the benchmark's own agent treats a live per-message spend figure as a
first-class part of the contract.

Our coverage on dsBN, the probe running with the first half of the repair:

| stage | with budget | total | |
|---|---|---|---|
| `plan` | 23 | 23 | 100 % |
| `plan_step` | 147 | 150 | 98 % |
| **`deep_research`** | **19** | **23** | **83 %** (was 0 %) |
| `propose` | 32 | 38 | 84 % |
| `foresight_rank` | 0 | 7 | — |
| `hyp_prioritize` | 0 | 8 | — |
| `novelty` | 0 | 5 | — |
| `hypothesis_merge` | 0 | 1 | — |
| **run total** | **221** | **255** | **87 %** |

The uncovered 13 % is not the same defect and is not being "fixed" today. Every one of those calls is
a bounded single shot with its own message list — including the four `deep_research` ones, which
turn out to be the claim VERIFIER ("You are a strict research verifier…"), not the tool loop. None of
them can loop, so none can spend a run. The stage that could, and did (`opus5`, §37.1), is the one
that now carries a figure that moves.

The remaining honest gap against the reference is 87 % versus ~100 %, and it is a difference of
architecture rather than of care: AlgoTuner has ONE message stream, so one wrapper covers it; we have
nine stages, four of which are single-shot rankers whose whole cost is 0.4–2.6 % of a run each.

### 41.1 An in-flight observation, recorded so it is checked and not assumed

dsBN spends **6.6 % / 22 calls** on `deep_research`, the lowest in its comparison group:

| probe | deep_research share | calls |
|---|---|---|
| dsBN (with the note) | **6.6 %** | 22 |
| dsNoStg3 | 11.1 % | 34 |
| dsNoStg | 11.6 % | 40 |
| dsFix1 | 12.2 % | 37 |
| dsFix4 | 15.3 % | 51 |
| dsNoStg2 | 15.7 % | 46 |
| dsFix2 | 21.0 % | 60 |

Tempting, and not a finding: dsBN is at $0.83 of $1 and unfinished, its share can still rise, and
n = 1. Written down now so the next sweep checks the finished number instead of remembering the
impression.

Also corrected here: I opened this sweep calling dsBN's node count an anomaly ("one node at $0.82
against six for the controls"). Wrong — the controls have **three** node DIRECTORIES each at $1.00
and dsBN has two at $0.83, which is on pace. The six came from §37's table, which counts node-creation
EVENTS, not directories. Two different metrics with the same name in my head.

## 42. dsBN sets the corpus record — and destroys §38's stage-guidance signal

`edge_expansion`, $1.008, two nodes, **test 344.4251** — the highest final on this task in the whole
corpus, ahead of dsNoStg3's 268.09. Both nodes shipped `solver_kernel.pyx` + `setup.py`: node_0
256.6588, node_1 **357.8648**, itself the corpus's highest single node. 108 min wall, 89 min LLM
(82 %), 1.4 min evaluation. Money: `plan_step` $0.5554 / 150 calls, `propose` $0.2256 / 71,
`deep_research` $0.0980 / 33, `plan` $0.0804 / 24.

**dsBN ran with the stage block ON.** Adding it to §38's comparison:

| stage block | n | finals | median |
|---|---|---|---|
| ON | 4 | 106.47, 132.70, 202.77, **344.43** | 167.73 |
| OFF | 3 | 153.40, 237.12, 268.09 | 237.12 |

The exact one-sided rank test over C(7,3) = 35 assignments now gives **p = 11/35 = 0.314**, against
0.100 yesterday. **§38's "suggestive at p = 0.10" is withdrawn.** One additional run in the ON arm
collapsed it, which is what a p of 0.10 at n = 3 vs 3 always meant.

One confound must be stated rather than buried: dsBN is not a pure ON control. It carried
`a78d295f`, the session-start budget note, which dsFix1/2/4 did not. So either that note is worth a
great deal at n = 1, or 344.43 is inside the ON band's natural spread and the band is simply wide.
**dsBN2 is running on the freed lane** to separate them — same task, same model, same $1, stage
block ON, and carrying `453c83d9` (the LIVE note). Against dsBN 344.43 it says whether the record
repeats; against dsFix1/2/4 it says whether the note is doing the work.

### 42.1 453c83d9 verified by probe: the figure now moves inside a session

The previous sweep's repair was committed unverified. dsBN2's first six `deep_research` generations:

    call 1: $0.0000    call 4: $0.0035
    call 2: $0.0011    call 5: $0.0049
    call 3: $0.0022    call 6: $0.0077

One new reminder per turn, appended only when the figure changed — against the seven identical
`$0.0000` lines dsBN showed under the session-start version. The fix does what it claimed.

§41.1's in-flight observation also resolves: dsBN's FINISHED `deep_research` share is **9.7 % over 33
calls**, not the 6.6 % / 22 seen mid-run. That is below all six controls (11.1–21.0 %) but only just,
at n = 1 — recorded, not concluded.

### 42.2 The reference caps history at five messages; we do not need to

`config.yaml` gives arm A `max_messages_in_history=5` — the agent is effectively memoryless beyond
its last five turns, relying on the file on disk. Ours carries far more. Measured whether that costs
us, over every $1 `edge_expansion` run with at least 20 generations (n = 28): median prompt growth
from the first fifth of a run to the last is **×1.08**, and the median LARGEST prompt any run ever
sends is **37,146 tokens** against the 131,072-token context. The worst case is dsFB2 at ×2.11 and
53,671 tokens, still under half the window.

So there is no runaway to cap. The reference's aggressive trim is a consequence of its architecture
(one linear message stream, no state outside the file), not a technique we are missing.

## 43. 5.2 % of arm A's money bought nothing, and it is our duty to say so

`check_money.sh` flagged a third `BrokenPipeError` and I went to dismiss it as transient for the
second time. Counting instead of dismissing: the ledger holds **96** of them across the whole
history, and they split into two completely different populations.

| | count | cost | completion tokens | shape |
|---|---|---|---|---|
| **arm A** | 29 (in attempt a3) | **$1.1875** | 4,592,307 (median 145,125) | cut MID-STREAM |
| **arm B** | 38 | **$0.0000** | 0 | died before the response started |
| probes | 29 | $0.0000 | 0 | same as arm B |

The arm-A ones arrive on a **precise 600-second period** — measured gaps of 601, 600, 601, 602, 600,
601, 605, 601, 601 s. That is a timeout, not model behaviour, and it is neither of the two we
control: the metering proxy's own is 1800 s (`proxy.py --timeout`) and the gateway's nginx
`proxy_read_timeout` is 300 s (recorded at `proxy.py:497`). A `BrokenPipeError` means the CLIENT went
away, so the 600 s cut is on AlgoTuner's side of the socket — most likely litellm's own request
timeout, which is a hypothesis and labelled as one.

**The money is inside the reported figure.** Arm A's a3 attempt spent **$23.0073 over 2,225 calls =
$1.1504 per task**, against the $1.19 the comparison reports — consistent. So arm A was billed for
those 4.59 M completion tokens and received none of them: **5.2 % of its budget, nearly one whole
task's worth**.

Arm B lost **nothing** to the same failure class. Our 38 broken pipes carry zero cost and zero
tokens because the connection died before any response began — a client-behaviour difference, not a
virtue.

This does not change any speedup number, and it is recorded because it runs AGAINST us: the
comparison is "equal money" in dollars billed, and in usable money arm A had about 5 % less. Anyone
reading the 4 wins / 3 ties / 3 losses table should carry this caveat with it.

I nearly filed this as "transient, both runs recovered" — for the second sweep running. What made
the difference was counting the population instead of looking at the three most recent rows.

## 44. All three "losses" re-probed: two flipped, one narrowed

dsDL closes the set. `discrete_log`, $1.003, two nodes, **test 14.5186**.

| node | speedup | eval s | submission | |
|---|---|---|---|---|
| node_0 | 0.0 | 504.0 | `_dlog.pyx` + `setup.py` | `evaluator_error` — the 120 s timeout cascade of §40.2 |
| **node_1** | **14.5385** | 35.4 | **`dlogc.pyx`** + `setup.py` | |

Money: `plan_step` $0.448 / 93 calls, `propose` $0.154 / 36, `deep_research` $0.125 / 37,
`repropose` $0.104 / 21. The run recovered from a total-loss node by rewriting the kernel under a
different name and evaluating in 35 s instead of 504.

**The consolidated picture across every task the plot report scored as a LOSS:**

| task | arm A | arm B | re-probed | verdict |
|---|---|---|---|---|
| `integer_factorization` | 9.763 | 9.147 | **194.82** (n=3: 96.21/178.76/194.82) | **20× win** |
| `discrete_log` | 1.542 | 1.211 | **14.52** | **9.4× win** |
| `kcenters` | 16.434 | 12.345 | 8 runs, unchanged | still a loss — numba ceiling, no `.pyx` ever written |
| `convex_hull` | 4.321 | 1.089 | **2.60** | narrowed 0.25× → 0.60×, still a loss; dsCH3 running at $3 |

Two of the four flipped outright and a third narrowed by 2.4×. The one that did not move is the one
where the pip repair provably changes nothing (§33: all eight kcenters runs reach their ceiling with
numba and none has ever written a Cython source).

Foreign champions on `discrete_log` mostly reach for `sympy.ntheory.residue_ntheory.discrete_log`
(Opus 4 does it in 17 lines; Opus 4.1 adds numba at 87). Ours is a Cython Pohlig-Hellman with
baby-step giant-step per prime-power factor — a different technique, and on this instance size it
wins.

**dsRBF launched on the freed lane**: `rbf_interpolation`, $1. It was a TIE in the report (arm A
1.058, arm B 1.047) and it is one of the three tasks carrying the unexplained aborts of docs/53 §9.
Now that §40.2 names the abort mechanism — one 120 s timeout, then AlgoTune's early-exit failing
every remaining run — this probe tests whether that explanation holds on a task known for it, and
whether the repaired stack moves a tie.

### 43.1 Our half of the same failure is our own 45-second first-byte guard, working

§43 established that arm B's broken pipes cost $0. This sweep asked WHY they exist at all, because
two more arrived 20 s apart on different probes and "transient" had already been the wrong answer
twice.

They come in **bursts across simultaneous probes** — 5 of 46 bursts hit more than one run:

| burst | probes |
|---|---|
| 08-27 22:24:28 | ds3, dsFBKc |
| 08-27 22:32:53 | ds3Hull, dsFBHull |
| 08-28 10:38:16 | dsFix1, dsFix2, dsN3, dsN3b (**four at once**) |
| 08-28 23:42:05 | dsBN, dsDL |
| 08-29 01:48:58 | dsBN2, dsCH3, dsIF3x |

Simultaneity across independent runs means a shared cause. Two candidates were tested and killed:

* **Not an evaluation fork-storm.** Zero evaluations were in flight at any burst — but the control
  says zero at random moments too (median 0.0 concurrent evaluations, mean 0.05), so this test has
  no power and is reported as uninformative rather than as a negative.
* **Not a proxy stall.** The ledger kept writing straight through every burst: 63–165 records in the
  ±5-minute window, largest gap 37–117 s against a 2.9 s median — normal for a stream of long LLM
  calls, and impossible if the proxy had frozen.

What remains fits every measurement: our client's **`llm_header_timeout = 45 s`** first-byte window
(`core/llm.py`, `httpx.Timeout(connect=...)`). When the upstream is briefly slow to start
responding, every concurrent run crosses 45 s at about the same moment, gives up, and the proxy
records `BrokenPipeError` when it finally has something to write. The 01:48:58 burst reads exactly
that way:

    01:48:19–48   heavy traffic, all 200, all billed
    01:48:58      dsCH3   BrokenPipe   $0.00000
    01:49:10      dsBN2   BrokenPipe   $0.00000
    01:49:18      dsIF3x  BrokenPipe   $0.00000
    01:50:03…     all three resume, all 200

**This is the guard working, not a defect.** Nothing was billed, nothing was lost, all three runs
retried. And it is the exact inverse of arm A's failure: theirs holds the socket for 600 s, pays for
4.59 M completion tokens and discards them; ours gives up at 45 s having paid nothing. The
asymmetry in §43 is a client-configuration difference, and this is the configuration that produced
it.

## 45. Three dollars bought less than one, on the task where one dollar wins by 20×

dsIF3x — `integer_factorization`, **$3.007**, six nodes, 456 min wall (75 % LLM, 4.8 min evaluation).
Champion node_4 at 156.2249 (`rho.pyx`); **test 155.0593**.

| budget | runs | finals | median |
|---|---|---|---|
| $1 | 3 | dsIF 96.21, dsIF2 178.76, dsIF3 194.82 | **178.76** |
| $3 | 1 | dsIF3x **155.06** | 155.06 |

Three times the money landed **below two of the three $1 runs**. The nodes show where it went:

| node | speedup | kernel |
|---|---|---|
| node_0 | 134.7222 | `rho.pyx` |
| node_1 | 33.5578 | `squfof.pyx` |
| node_2 | 0.0 | — |
| node_3 | 109.1567 | `fastrho.pyx` |
| node_4 | **156.2249** | `rho.pyx` |
| node_5 | 113.5379 | — |

The run reached 134.72 on its FIRST node and spent the remaining ~$2.5 producing one node 16 % better
and four worse — including a total loss and two detours into different factoring algorithms
(`squfof`, `fastrho`) that both underperformed the Pollard rho it started with. `plan_step` took
$1.579 over 344 calls, more than a whole $1 run's entire budget.

This is one run against three, so it is not a law. But it is the first direct evidence on the
spend-vs-quality question and it points the unwelcome way: on a task where the first node already
lands near the ceiling, extra budget buys exploration that mostly moves sideways. **dsIF4 launched on
the freed lane** — a fourth $1 run on the same task, to put the $1 arm at n = 4 before anything is
concluded from a single $3 point.

### 45.1 dsRBF's zero is a DIFFERENT failure from dsDL's, and they printed the same word

Both nodes scored 0.0 with `reason: evaluator_error` and the same "Unexpected results format"
verdict. The payloads are opposite:

| probe | eval s | `error_type` | `num_errors` | `num_timeouts` | what the model should do |
|---|---|---|---|---|---|
| dsDL node_0 | 504.0 | timeout | 0 | >0 | make it **faster** |
| dsRBF node_0 | 35.6 | `execution_error` | 3 | 0 | make it **correct** |

dsRBF's solver raised its own `LinAlgError("Singular matrix in RBF solve.")` three times out of
three runs and the evaluation stopped on "Critical execution error". Nothing was slow.

Fixed in `2a9dd4f6`: `failure_shape()` lifts `error_type` / `runs` / `num_errors` / `num_timeouts`
out of the harness payload into the `no_speedup` block. The reason VOCABULARY is untouched on
purpose — it is registry-guarded and inventing a word is a bigger change than surfacing evidence
that already exists. Absent evidence yields `{}`, never a block of zeros, so "no data" cannot read
as "zero timeouts". Three mutations redden.

## 46. The stage switch is settled at "no effect"; the budget note now looks real

dsBN2 — `edge_expansion`, $1.013, three nodes climbing 19.6656 → 147.0915 (`edge_expansion_fast.pyx`)
→ **223.3369** (`edge_kernel.pyx`). **Test 221.8235.** `plan_step` $0.377 / 117 calls, `propose`
$0.207 / 67, `plan` $0.184 / 47, `deep_research` $0.098 / 34.

### 46.1 Stage guidance: p = 0.286 at n = 5 vs 3, and the trajectory says why to stop

| stage block | n | finals | median |
|---|---|---|---|
| ON | 5 | 106.47, 132.70, 202.77, **221.82**, **344.43** | 202.77 |
| OFF | 3 | 153.40, 237.12, 268.09 | 237.12 |

**p = 16/56 = 0.286.** The full trajectory of this claim, one sweep at a time:

    n=3 vs 3   p = 0.100   "suggestive"
    n=4 vs 3   p = 0.314   withdrawn
    n=5 vs 3   p = 0.286   stable at no effect

The ON median moved 132.70 → 202.77 as the two newest runs joined it. The switch stays in the code
as an operator opt-out — it demonstrably removes ~5,001 characters nothing called — but the claim
that removing them HELPS is dead, and this is the third sweep in a row that says so more firmly.

### 46.2 The budget note is associated with less research, and the number repeats exactly

Both runs carrying `a78d295f`/`453c83d9` spend the same share on `deep_research`, and both sit
strictly below every control:

| run | note | `deep_research` share | calls |
|---|---|---|---|
| **dsBN** | yes | **9.7 %** | 33 |
| **dsBN2** | yes | **9.7 %** | 34 |
| dsNoStg3 | no | 11.1 % | 34 |
| dsNoStg | no | 11.6 % | 40 |
| dsFix1 | no | 12.2 % | 37 |
| dsFix4 | no | 15.3 % | 51 |
| dsNoStg2 | no | 15.7 % | 46 |
| dsFix2 | no | 21.0 % | 60 |

The honest test is against the three controls with the SAME stage setting (dsFix1/2/4, median
15.3 %): both note-runs rank first and second, exact one-sided **p = 1/10 = 0.100** at n = 2 vs 3.
Against all six controls it is 1/28 = 0.036, but that pool mixes stage-off runs and is the weaker
comparison, not the stronger one.

**p = 0.100 is exactly the threshold that just collapsed for §46.1**, so it is reported as
"promising, undecided" and nothing is concluded. **dsBN3 launched on the freed lane** — a third
note-run at the same settings, which takes the clean comparison to n = 3 vs 3.

A correction to my own arithmetic: the first version of this test printed `p = 0.000`, which is
impossible at n = 2 vs 3 (the floor is 1/10). Two identical 9.7 % values collapsed into one rank in
my tie handling. The corrected figures are above.

## 47. dsRBF is the first run the novelty gate ate whole

`rbf_interpolation`, **$1.006, ONE node, metric 0.0** — a total loss, the second in the corpus after
`opus5` (§37.1). 96 min wall, **95 % LLM, 0.6 min evaluation**. Phases: `plan_step` $0.4011 / 87,
`propose` $0.2207 / 59, `plan` $0.1484 / 36, `repropose` $0.0985 / 26, `deep_research` $0.0802 / 27.

The event stream names the mechanism exactly:

    node 0  propose   finished  462.4 s  ok=true    → node built, scored 0.0 (execution_error)
    node 1  propose   finished 2254.5 s  ok=FALSE
    node 1  novelty   finished  850.8 s  ok=FALSE   ← novelty_rejected
    run_finished  reason=budget_exhausted  "$1.0033 of the $1.0000 set by llm_budget_usd"

Forty-five of the run's ninety-six minutes went into proposing, and the second proposal — 2,254 s of
it — was **discarded by the novelty gate**, after which the money ran out. The report the run wrote
for itself says it plainly: *"#0 is the champion at metric=0 (draft); written without the model."*

**This is not a new defect. It is a known price, and the config already carries the measurement.**
`core/config.py` on `novelty_mode="llm"` (the default) records, from the 20-run arm-B campaign: 99
invocations, 823 paid calls, **$1.77 of a $15.73 campaign and 6.6 of its 60.8 run-hours, for 10
rejections**; an admitted proposal costs a median 4 calls / 10.6 s, a REJECTED one **37 calls /
21.6 minutes**. The same note concludes the gate is **net-negative on AlgoTune** ($1.77 and 6.6 h
spent to avoid ~$0.77 and 5.7 min of duplicate evaluation) and deliberately leaves the product
default alone, because flipping it on one task family's evidence would be the same error as fencing
a proposal on the log's length.

What dsRBF adds is the tail of that distribution: the first run where the gate consumed the ENTIRE
budget and left nothing. Corpus-wide the gate fired 48 times across 29 of 54 runs; failed-phase
wall-clock totals 4,803 s in `propose` and 1,082 s in `novelty`, and **dsRBF alone is 3,104 s of
that 5,885 — 53 %**. Its `repropose` count (26) is exactly the corpus MEDIAN, so it is an outlier in
time, not in attempts.

**dsNov launched on the freed lane** with the operator lever the config names — `novelty_mode=off`,
confirmed in the run's own `config.snapshot.json`. It is `edge_expansion`, $1, otherwise identical
to dsBN/dsBN2/dsBN3, so it differs from them by exactly one setting. This is the lever being
verified BY PROBE rather than by re-reading the note that recommends it.

<!-- Sections 48-71 were reconstructed on 2026-08-30 from the session transcript, after the
     2026-08-29 container restart destroyed the working tree that held them. The prose is the
     transcript's verbatim heredoc text; the section-to-commit map is in the recovery commit's
     message. Nothing here was re-derived or paraphrased -- where the transcript was silent, the
     recovery says so rather than filling in. -->

## 48. The night's four finals: one loss closed, one claim weakened, one signal at p = 0.05

| probe | task | budget | test | what it was for |
|---|---|---|---|---|
| **dsCH3** | `convex_hull` | **$3** | **12.1764** | is the last loss a budget shape? |
| dsIF4 | `integer_factorization` | $1 | 79.5759 | fourth $1 point against the $3 one |
| dsBN3 | `edge_expansion` | $1 | 212.8573 | third budget-note run |
| dsNov | `edge_expansion` | $1 | 235.1685 | `novelty_mode=off`, n = 1 |

### 48.1 `convex_hull` — the last unclosed loss, closed with money

| | speedup |
|---|---|
| arm A (AlgoTuner) | 4.321 |
| arm B (campaign) | 1.089 |
| dsCH, $1 | 2.5955 — **a loss**, 0.60× of arm A |
| **dsCH3, $3** | **12.1764** — **a 2.8× win** over arm A |

§40 read the $1 result as a budget shape rather than a capability gap: `$0.62` of the dollar went
before the first node existed, evaluation costs 72.8 s, and one node is all a dollar buys on 267,021
instances. Three dollars bought five nodes and the reading holds. **Every task the plot report
scored as a loss has now been re-probed, and three of the four flipped**: `integer_factorization`
(20×), `discrete_log` (9.4×), `convex_hull` (2.8×). Only `kcenters` stands, and that is the one task
where the pip repair provably cannot help (§33). n = 1 at $3, so dsCH4 is running to repeat it.

### 48.2 §45 weakened by its own fourth point

dsIF4 came in at **79.5759**, the lowest $1 run on the task. The $1 arm is now
79.58 / 96.21 / 178.76 / 194.82 (median 137.48) and the single $3 run's **155.06 sits INSIDE that
range**. §45's headline — "three dollars bought less than one" — was written at n = 3 vs 1 and is
now "indistinguishable at n = 4 vs 1". The spend-vs-quality question is open again, and this is the
third claim this campaign has had to withdraw on its own next measurement.

### 48.3 The budget note reaches p = 0.050, the floor at this sample size

All three runs carrying `a78d295f`/`453c83d9` beat all three controls at the same settings:

| with the note | without |
|---|---|
| 344.4251 | 202.7654 |
| 221.8235 | 132.70 |
| 212.8573 | 106.4716 |
| **median 221.82** | **median 132.70** |

Rank sum **15 of a possible 15** — a perfect separation — and the exact one-sided test over all
C(6,3) = 20 assignments gives **p = 1/20 = 0.050**, the smallest value n = 3 vs 3 can produce.
Median ratio **×1.68**.

Set against the stage-guidance switch, which this document tracked through 0.100 → 0.314 → 0.286 and
then abandoned: same threshold at the start, opposite trajectory. **A caveat that must travel with
this number**: new controls can no longer be produced, because the note has no off switch — it ships
unconditionally. The three controls are older runs, so this is a before/after comparison, not a
randomised one, and everything else that changed between those dates is confounded with it. dsBN4 is
running to take it to n = 4 vs 3, where the floor becomes 1/35 = 0.029.

`dsNov` (`novelty_mode=off`, confirmed in its own snapshot) landed at **235.1685** with the note also
present, so it belongs to neither arm above. dsNov2 is running for n = 2.

## 49. The novelty gate is free to turn off, and `read_code` was blind for 2,486 calls

### 49.1 dsNov2 closes the gate question on MONEY, not on the number

`edge_expansion`, $1.007, three nodes 110.5775 → **250.6628** (`edge_expansion_kernel.pyx`) →
32.7723; champion node_1, **test 251.86**. Its `novelty` and `repropose` phases cost **$0.0000 over
0 calls** — the `novelty_mode=off` lever does exactly what its config note says.

| run | gate | `novelty`+`repropose` | share of run | final |
|---|---|---|---|---|
| dsNov | off | **$0.0000** | 0.0 % | 235.17 |
| dsNov2 | off | **$0.0000** | 0.0 % | 251.86 |
| dsBN | llm | $0.0035 | 0.3 % | 344.43 |
| dsBN2 | llm | $0.0995 | **9.8 %** | 221.82 |
| dsBN3 | llm | $0.0909 | **9.1 %** | 212.86 |

Medians: off **243.51** (n = 2), llm **221.82** (n = 3). The ranges overlap heavily — the highest
run of all, 344.43, is an `llm` one — so **the final number does not decide this at n = 2 vs 3 and
is not claimed to**. What is decided is the price: ~9 % of a $1 run, returned in full, on a task
family where `core/config.py`'s own 20-run measurement already found the gate net-negative
($1.77 of $15.73 and 6.6 of 60.8 hours, for 10 rejections). The operator lever works; the case for
pulling it is the money, and it is measured twice now.

### 49.2 `read_code` returned a filename it made up, 2,486 times

Following the second concrete complaint in the corpus (`glm53f`: "I can't read node 0's code —
read_code returned essentially empty") to its cause: **2,486 of 2,492** `read_code` calls came back
under 200 characters, every one of them

    # solution.py of experiment #0
    other files: ['solver.py']

The method printed a HARDCODED `solution.py` header, then `n.code`, then the NAMES of `n.files`. On
every AlgoTune node `n.code` is empty — the work lives in `files` — so the answer was a wrong
filename and a list of filenames, and the `no code recorded` guard could not fire because `n.files`
is non-empty. `read_code` is the third most-called tool in `propose` (764 calls) after `repo_read`
and `read_experiment`, so the phase whose whole job is to improve existing code has been proposing
without seeing it for the entire corpus.

Fixed in `d066da36`: the files are printed, solver-bearing first so a spent budget never cuts the
file the reader came for, each with its real name and size, truncation marked, and anything left out
NAMED rather than counted. Three mutations redden; the 767 tests around `run_tools`, the digest and
the cross-run readers stay green.

**dsRC launched** carrying this fix AND the card fix of `2d3c2d43` (verified in its own card text),
against dsBN/dsBN2/dsBN3 — 344.43 / 221.82 / 212.86 — which ran with a blind `read_code` and a card
that told a Researcher to run a Developer's command. Neither fix has been verified by probe until it
finishes, and both are stated as unverified until then.

## 50. The measurement was the problem: `edge_expansion` at $1 is BIMODAL, and n = 3 cannot see past it

dsBN4 finished at **test 27.5655** — the fourth budget-note run, and the lowest. Its two nodes say
what happened: node_0 wrote `cutcount.pyx`, the kernel BUILT (`build_ext ok`) and computed **wrong
values** (`no_valid_speedups`; proposed 90.75 against a reference 14.234), scoring 0.0; node_1 then
retreated to pure Python and scored 27.31. A working build that produces wrong numbers sent the run
back to the pre-repair band.

**§48.3's p = 0.050 is withdrawn.** With dsBN4 the budget-note arm is 27.57 / 212.86 / 221.82 /
344.43 against controls 106.47 / 132.70 / 202.77, rank sum 19 of 22, exact one-sided
**p = 7/35 = 0.200**. That is the fourth claim this campaign has retracted on its own next
measurement, and the second switch to go from "significant at n = 3" to nothing on point four.

### 50.1 Why every one of these signals collapsed

All 27 `edge_expansion` runs at $1 on deepseek-v4-flash, sorted:

    344 268 252 237 235 229 222 213 207 203 153 133 106 | 48 32 30 29 28 28 28 28 28 28 27 27 19

Range **18.5 – 344.4**, ratio **19×**, coefficient of variation **87 %**. That is not scatter around
a mean — it is two populations, and the variable that separates them is one bit:

| does the CHAMPION ship a compiled kernel | n | median | range |
|---|---|---|---|
| yes | 14 | **217.3** | 27.8 – 344.4 |
| no | 13 | **27.8** | 18.5 – 48.5 |

An **8×** median gap, and the groups barely touch. So a comparison of final speedups at n = 3 is a
comparison of how many runs in each arm happened to land the high mode. Inside the repaired arms the
rate is 11 of 12 champions, so the modes are no longer a coin flip — but **one mode-miss is worth
more than any switch**, and dsBN4 alone moved p from 0.050 to 0.200.

**The methodological conclusion, which corrects how this document has been arguing for four
sweeps**: no loop-switch A/B on `edge_expansion` at $1 is decidable at n ≤ 4 by comparing finals.
The stage block (0.100 → 0.314 → 0.286), the budget note (0.100 → 0.050 → 0.200) and the novelty
gate (overlapping at n = 2 vs 3) all measured the same thing — mode assignment — and none measured
its switch. What IS decidable at this sample size is the money (the novelty gate's ~9 % of a run,
§49.1) and the bit itself: whether the champion carries a working kernel.

Two consequences, and both are changes to what gets measured rather than to the loop:

* Future switch comparisons on this family report the **kernel-landing rate** — a binary outcome —
  beside the median, because the rate is what the median is actually made of.
* `integer_factorization` is the tighter ruler for the same question: its four $1 runs span
  79.58 – 194.82, a ratio of **2.4×** against edge_expansion's 19×.

**dsRC2 launched** beside dsRC so the `read_code` + card verification starts at n = 2 rather than
n = 1 — which is the first thing this section says is worthless.

## 51. dsIF5, the train→test question nobody had asked, and a zero that had a name

dsIF5 — `integer_factorization`, $1.012, three nodes 104.9736 → **208.5143** (`_pollard_rho.pyx`) →
124.0001, champion node_1, **test 286.5205**. 149 min wall, 86 % LLM, 1.6 min evaluation. It is the
task's best result and its champion carries a working kernel.

**Correction to §50's recommendation.** I called `integer_factorization` the tighter ruler at "2.4×"
— that was n = 4. At n = 5 it is 79.58 / 96.21 / 178.76 / 194.82 / 286.52, ratio **3.6×**. Still six
times tighter than `edge_expansion`'s 19×, and the mode gap is smaller too (the one champion without
a kernel, dsIF4 at 79.58, sits 2.3× below the kernel median rather than 8× below), so the
recommendation holds with the corrected number. The $3 run's 155.06 is now BELOW the $1 median of
178.76.

### 51.1 Train → test drift, measured for the first time

Every number this document compares is a TEST final, while the loop optimises on TRAIN. That
relation had never been measured. Across all 54 probes with both figures, `test / best-train-node`:

| | value |
|---|---|
| median | **0.986** |
| mean | 0.973 |
| range | 0.000 – 1.374 |
| runs where test < best train | **42 of 54** |

So there is no systematic drift worth correcting for — the mild pessimism is what selecting a
maximum on one split and reporting the other does. Per task:

| task | n | median | range |
|---|---|---|---|
| `integer_factorization` | 6 | **0.993** | 0.985 – 1.374 |
| `edge_expansion` | 32 | 0.989 | 0.000 – 1.015 |
| `convex_hull` | 6 | 0.978 | 0.949 – 1.009 |
| `kcenters` | 8 | 0.926 | 0.905 – 0.955 |

`integer_factorization` has the best median AND the widest positive tail (dsIF3 ×1.27, dsIF5 ×1.37)
— its graded set is four instances, so a split difference moves the number. Worth carrying when its
finals are read.

### 51.2 The 0.000 in that range is ds3, and it cost a whole run

`ds3` folds to **train 156.4328 → test 0.00**. Its champion, node 0, shipped `cutcounter.pyx` with
**no `setup.py`** — the bridge of the day answered `build_ext failed rc=2` about a file the model
never wrote (the message `3be289eb` replaced), nothing built the extension, and the solver's import
of it failed on the graded pass. **Node 2 of the same run shipped a recipe and scored 142.3965**, so
the loop had a working alternative and champion selection, reading the train metric alone, took the
broken one.

Fixed in `8604dfbc`: `extract_champion.py` now WARNS, on the bridge's own condition, that the
champion ships a Cython source nothing will build and that the graded pass will score it 0.0. It
warns rather than refuses — the choice is the loop's and `campaign.sh` branches on the exit code —
but the warning arrives before the zero instead of after it.

**dsIF6 launched** on the freed lane: `integer_factorization` at **$3**, the second point on the
spend curve for this task, against the $1 arm's five (median 178.76) and dsIF3x's single 155.06. It
is deliberately on the task §50 identified as the tighter ruler rather than on `edge_expansion`,
where §50 showed a comparison at this sample size cannot mean anything.

## 52. `read_code` verified BY PROBE, and the last reference command answered

### 52.1 The fix works, measured on running probes rather than argued

`read_code` (`d066da36`) needed a probe, not a claim. dsRC and dsRC2 carry it; dsBN*–dsBN4 ran with
the old one. Both sets are on the same task, model and budget, so this is the same call in the same
place before and after:

| | calls | returned under 200 chars | median length |
|---|---|---|---|
| before (dsBN, dsBN2, dsBN3, dsBN4) | 71 | **71 (100 %)** | **87** |
| after (dsRC, dsRC2) | 24 | **0 (0 %)** | **2,817** |

A **32×** increase in what the phase is handed, and the content is the solver's own source rather
than a header and a filename. This does not need the runs to finish and is not waiting on them; the
finals will say whether it changes the NUMBER, which §50 warns cannot be decided at n = 2 anyway.

### 52.2 `revert`, the last AlgoTuner command with no counterpart here

The reference agent's ten commands have now all been walked. `revert` was the last one left, and it
restores its single mutable `solver.py` from a snapshot. We have no such command, and the corpus
says we need no such command:

* **`node_repaired` fires ZERO times** across all 57 probes. Nothing is ever edited in place; every
  node is a fresh directory and a fresh submission, so there is no editor state to roll back.
* The 1,200 "revert" mentions in model reasoning are the model's own PLAN language — "if it fails,
  revert", "revert to the baseline (which still exists as node #0)" — not requests for a tool. One
  states our architecture back to us: *"each experiment starts from a clean slate and the Developer
  re-implements"*.

So the operation `revert` performs for AlgoTuner — **get me the previous version of the code** — is
performed here by `read_code` against the earlier node. Which is exactly the call that returned
nothing 2,486 times. The gap was never a missing command; it was the substitute being broken, and
§52.1 is the measurement that it no longer is.

That closes the command-surface comparison begun in §36: of AlgoTuner's ten, `profile`/`profile_lines`
were measured and declined (§38.1, no signal), the editor's syntax gate was measured and declined
(§33.1, zero offenders), the validator's line-level context is unreachable through the script we call
and would have fired zero times (§36), and `revert` needs no counterpart. Nothing on that surface is
outstanding.

## 53. dsRC, and a correction to every "% of wall clock" this document has printed

dsRC — `edge_expansion`, $1.022, three nodes 24.8688 → **149.3394** (`edge_cut.pyx` + `setup.py`) →
140.9803; champion node_1, **test 149.5423**, kernel at the champion. It is the first run carrying
BOTH of yesterday's fixes: the rewritten `read_code` (`d066da36`) and the card's tool-holder sentence
(`2d3c2d43`). Money: `plan_step` $0.392 / 113, `plan` $0.189 / 49, `deep_research` $0.175 / 57,
`propose` $0.167 / 50.

Against the pre-fix arm — dsBN 344.43, dsBN2 221.82, dsBN3 212.86, dsBN4 27.57 — dsRC's 149.54 sits
inside the range and **decides nothing at n = 1**, which is what §50 says about this task at this
sample size and is not walked back here. What IS decided is §52.1's measurement, which needed no
finals: `read_code` returns real source now, 0 empty of 24 against 71 of 71 before.

### 53.1 The number that could not be true

dsRC's time analysis printed **"LLM 95 min (101 % of wall)"** over a 94-minute run. Generations
OVERLAP — speculation runs up to two at once — so `sum(duration_s)` double-counts and is not a
fraction of anything. Measured over 60 probes:

| | |
|---|---|
| median overlap | **0 %** |
| range | 0 – 16.3 % |
| runs where the naive sum EXCEEDED the wall clock | **6** — dsNov2 105 %, dsRC 101 %, dsFix4 101 %, dsNoStg2 101 %, dsBN4 100 % |

**So every "LLM X % of wall" figure in §9, §34, §45, §47 and §51 is inflated by that run's overlap.**
For most runs the correction is zero; for the six above it is 9–16 points. dsRC's real occupied time
is 85 of 94 minutes — **90 %**, not 101 %. The shape of those sections' argument does not change (the
loop is LLM-bound and evaluation is 1–5 % of wall), but the figures were arrived at by a method that
could produce an impossible answer, and did, five more times than I noticed.

The committed script was NOT the source: `plot_corpus_v2.py` stores `llm_s` and never divides it by
`wall_s`. The error lived in the ad-hoc sweep analysis — i.e. in this document's prose. So the
durable fix puts the right quantity where a future reader will find it: `_union_seconds()` merges
the intervals and `llm_busy_s` sits BESIDE `llm_s`, because the sum is a real quantity too — what
the provider billed wall-clock for — and only the union may be divided by a run's wall clock
(`e1715704`, mutation-checked).

**dsRC3 launched** on the freed lane, taking the `read_code` + card verification to n = 3 against the
four pre-fix runs — the sample size §50 says is the minimum worth arguing from, and still short of
what that section says would settle it.

## 54. A 1.3 GB core dump the sweep's own check had never seen fire

The core-dump line of the liveness check has printed `0` on every previous pass. This one printed
**1**: `AlgoTune/core`, **1,316,196,352 bytes**, written at 10:57:52 by `AlgoTune/.venv/bin/python`
with a candidate's `bsp_wrap.cpython-311-x86_64-linux-gnu.so` loaded. A compiled kernel killed the
arena's evaluator by signal and it dumped its whole address space into the CWD.

The evaluation **survived** — the harness isolates the crash and the score arrived — so the harm is
purely disk: twenty such runs is 26 GB on a box with 78 GB free, and the campaign runs four lanes at
once.

`looplab_check.py::_run_isolated` has capped `RLIMIT_CORE` since 2026-08-28, for this exact reason
and with a measured 1.4 GB behind it. The REAL evaluator did not — **the cheap checker was protected
and the expensive one was not**. Fixed in `e513c89f`: `preexec_fn` sets the limit in the child
between fork and exec, best-effort.

Its second test is not a claim about a flag: it runs a child that really dies of SIGSEGV, capped and
uncapped, and asserts the capped one leaves nothing. It skips loudly where `kernel.core_pattern`
pipes cores to a helper, because there neither arm writes a file and the guard would pass vacuously
in both directions. On this host it did not skip.

## 55. dsRC2, and the verification arm stated at the size it actually is

dsRC2 — `edge_expansion`, $1.016, three nodes **208.1972** (`edge_cut.pyx` + `setup.py`) → 162.5632 →
19.1604; champion node_0, **test 206.3095**, kernel at the champion. Money: `plan_step` $0.359 / 115,
`propose` $0.212 / 53, `repropose` $0.158 / 34, `plan` $0.157 / 40. Time, computed the way §53.1 says
it must be: wall 118 min, LLM **occupied** 107 min = **91 %** (union of the generation intervals, not
their sum), evaluation 2.1 min.

The `read_code` + card verification arm now reads:

| | runs | median |
|---|---|---|
| after the fixes | 149.54, 206.31 | 177.93 |
| before | 27.57, 212.86, 221.82, 344.43 | 217.34 |

Rank sum 5, exact one-sided **p = 13/15 = 0.867**. **This decides nothing**, and by §50's own
arithmetic it could not: at n = 2 vs 4 on a task whose outcome is a bimodal bit with an 8× gap, the
test has no power in either direction. Reported here so the arm is on record at the size it actually
is, rather than left to look like a pending win. What IS established, and needed no finals, is
§52.1: `read_code` returns real source now — 0 empty of 24 against 71 of 71 before.

**dsRC4 launched** beside dsRC3, taking that arm toward n = 4 vs 4.

## 56. The card fix verified by probe — and the first attempt to verify it was contaminated by the fix

§52.1 verified `read_code` by probe. The card fix of `2d3c2d43` had not been, so this sweep did it.
The claim was narrow: a Researcher should stop spending turns discovering that
`run_dev_command(...)` — named in the card, addressed to the Developer — is not on its tool surface.

**The first measurement said the fix made things WORSE**: 9 of 224 generations after against 9 of
311 before, i.e. 4.0 % against 2.9 %. Reading the "after" example explains it —

> my list. The user turn mentions `run_dev_command` but says **"they are not on your tool surface"**
> — **"THE COMMANDS BELOW ARE THE DEVELOPER'S. If you are proposing rather…"**

The model was quoting **the sentence I added**, and my pattern matched my own text. The instrument
was contaminated by the thing it was measuring. Separating "discovers the contradiction" from
"quotes the new clause and moves on":

| | generations | confusion | quotes the clause |
|---|---|---|---|
| after (dsRC, dsRC2, dsRC3, dsRC4) | 226 | **0 (0.0 %)** | 8 |
| before (dsBN, dsBN2, dsBN3, dsBN4) | 311 | **2 (0.6 %)** | 0 |

So the fix does what it claims. **And what it claims is worth very little.** Priced out on the
measured rate of $0.14/Mtok:

* cost — 253 characters ≈ 63 tokens in every card-bearing prompt, 645 of them across four probes:
  **$0.0014 per run**;
* benefit — two avoided `propose` generations per ~311, at a median `propose` cost of ~$0.004:
  roughly **$0.008 per run**.

Six times its own price, and both numbers are thousandths of a dollar on a $1 run. It stays because
it is correct and cheap, not because it matters. Recorded at that size rather than as a win — the
same discipline §55 applied to the arm it belongs to.

**What this sweep did NOT find.** No new loop defect: the reference-agent command surface was closed
in §52.2, the corpus's own "I can't" complaints were mined in §49.2 and §36, and the one anomaly the
liveness list surfaced (the 1.3 GB core) was fixed last sweep. The work here was verifying two fixes
by probe, which is what the rule asks for before either is believed.

## 57. §50 pooled two eras, and that is where its bimodality came from

§50 measured all 27 `edge_expansion` runs at $1 together, found a 19× spread, CV 87 % and a clean
two-population split on "does the champion ship a kernel", and concluded that no switch comparison is
decidable at n ≤ 4. The split was real. The **pooling** was the mistake, and this sweep found it by
asking which runs failed to write a `.pyx` at all:

| era | runs | never wrote a `.pyx` | champion has a kernel | median |
|---|---|---|---|---|
| before the pip repair (2026-08-28 08:49) | 14 | **10** | 2 (14 %) | **28.0** |
| after | 15 | **0** | 14 (93 %) | **206.3** |

Before the repair a `.pyx` could not be built at all (§33: `pip install .` answered "No module named
pip" 363 times), so ten runs correctly did not write one. Those ten ARE the low mode. Pooling them
with the current era manufactured a coin flip that no longer exists.

**The corrected noise floor**, post-repair only:

| | n | CV | range |
|---|---|---|---|
| all post-repair runs | 15 | 47 % | 27.6 – 344.4 (**12.5×**) |
| conditional on the champion landing a kernel | 13 | **30 %** | 106.5 – 344.4 (**3.2×**) |
| (pooled, as §50 reported it) | 27 | 87 % | 18.5 – 344.4 (19×) |

So the mode-miss rate is **2 of 15 = 13 %**, not the ~48 % the pooled figure implied, and conditional
on landing, `edge_expansion` is a 3.2× ruler — the same order as `integer_factorization`'s 3.6× (§51),
not six times worse.

**What this changes.** §50's mechanism stands: the kernel bit dominates and a mode-miss outweighs any
switch. What does not stand is its counsel of despair. At a 13 % miss rate and CV 30 % conditional on
landing, an exact rank test at **n = 5 vs 5** has a floor of 1/252 = 0.004 and enough resolution for a
1.5× effect to clear it — reachable in two sweeps per arm rather than never. The revised rule for
this family:

* report the kernel-landing rate beside the median, as §50 already required;
* **and never pool across the pip repair** — the eras are different instruments, and every figure in
  this document computed over "all runs" of `edge_expansion` should be read as a mixture until it is
  split. §50's own headline is the first casualty and is corrected here rather than left standing.

Nothing else in the document mixes the eras: §33's kernel-vs-no-kernel medians (192.32 / 25.41) are
the STATEMENT of the era difference rather than a victim of it, and every switch arm (dsFix*, dsBN*,
dsNoStg*, dsNov*, dsRC*) is post-repair throughout.

## 58. Three kernel rewrites out of four, and a hypothesis that did not survive

§57 left the kernel bit as the thing that decides a run. This asks what happens to a kernel once a
run has one. Across every node-to-node transition where both nodes ship a `.pyx` — 43 of them:

| | |
|---|---|
| median body similarity (`difflib`, char-level) | **0.35** |
| near-identical (> 0.9) | **5 %** |
| rewritten from scratch (< 0.5) | **77 %** |

So a working kernel is inherited **once in twenty**. Three transitions in four throw the previous
`.pyx` away and write a new one, and each rewrite is a fresh chance to lose the kernel entirely
(dsBN4 did exactly that: `cutcount.pyx` built, computed 90.75 against a reference 14.234, scored
0.0, and node_1 retreated to pure Python at 27.31) or to land in the 2-of-75 that build and compute
wrong.

**A first measurement by FILENAME said 47 %, and that number is wrong** — it counted renames, not
rewrites. `dsIF3` goes `rho.pyx` → `factor_cy.pyx` → `rho.pyx`, which the filename metric reads as
two rewrites and one return; the bodies say all three are different code. Names are not content, and
the figures above are the bodies.

### 58.1 The obvious explanation, tested and not supported

`read_code` returned nothing 2,486 times (§49.2). The natural hypothesis is that the model rewrote
because it could not SEE the previous kernel, and the prediction is that similarity rises once the
reader works. Split at `d066da36`:

| | transitions | median similarity | rewritten (< 0.5) |
|---|---|---|---|
| before the fix | 39 | 0.37 | 74 % |
| after | **4** | 0.27 | 100 % |

**Not supported.** At n = 4 the two medians are indistinguishable and the direction is the wrong one
anyway. Rewriting is the loop's habit, not a consequence of the blind reader, and the `read_code`
fix should not be expected to change it. Recorded as a failed prediction rather than quietly
dropped — it is the fifth this campaign has had to retract, and the cheapest, because it cost one
query.

**Is the rewriting a defect?** Not one this sweep can fix. Each node is an independent experiment
with a fresh submission by construction, which is the same property that makes `node_repaired` fire
zero times (§52.2) and makes `revert` unnecessary. Changing it is an architecture decision, not a
sweep-sized repair. What is worth carrying forward is the number: a run that lands a kernel keeps it
5 % of the time, so the kernel bit §57 measures is re-rolled at almost every node rather than won
once.

## 59. §44 called `kcenters` a loss. It is a 7.4× win, and was one before the repair

§44's consolidated table carries this row:

> \| `kcenters` \| 16.434 \| 12.345 \| 8 runs, unchanged \| still a loss — numba ceiling, no `.pyx`
> ever written \|

**"Unchanged" is right and "still a loss" is wrong**, and the two got welded together. The pip
repair genuinely does not move `kcenters` — §33 measured that, and the reason is that every run
reaches its ceiling with numba and none has ever written a Cython source. But that is a statement
about the REPAIR, not about who wins. The eight probes:

    171.78  171.15  162.13  159.51  84.62  45.08  37.82  30.68

Median **122.1** against arm A's **16.434** — a **7.4× win**, and **even the worst of the eight beats
arm A by 1.9×**. Seven of the eight predate the pip repair, so this was a win before anything was
fixed.

What the row was actually reading is the campaign's arm-B figure, 12.345. That number is one run of
ours on a broken stack; it is not our capability, and putting it in the "re-probed" column's place
made a re-probe look pending when eight of them were already on disk.

**So all four tasks the plot report scored as losses have flipped**, not three:

| task | arm A | our probes | verdict |
|---|---|---|---|
| `integer_factorization` | 9.763 | 79.58 – 286.52 (n = 5) | **up to 29× win** |
| `discrete_log` | 1.542 | 14.52 | **9.4× win** |
| `kcenters` | 16.434 | 30.68 – 171.78 (n = 8) | **7.4× win at the median** |
| `convex_hull` | 4.321 | 2.60 at $1, **12.18 at $3** | **2.8× win at $3**, a loss at $1 |

The `convex_hull` row keeps its caveat: at the $1 the comparison was run at, it loses; the win needs
$3 (§48.1), and dsCH4 is the second $3 point.

**dsKc2 launched** — `kcenters` at $1, the second POST-repair run of that task against dsFixKc's
159.51, so the family stops resting on seven pre-repair numbers. **dsPde launched** — `pde_heat1d`
at $1, a task never probed and one of the three carrying docs/53 §9's unexplained aborts, so §40.2's
timeout-cascade explanation meets a task it was not derived from.

## 60. Auditing my own table against the corpus: two more rows were wrong

§59 found that §44's `kcenters` row welded "the repair does not move it" to "we lose there". That was
one row. This sweep re-derived **every** row from the probes rather than from memory, comparing the
median of our runs on each task against arm A:

| task | arm A | probes | median | verdict |
|---|---|---|---|---|
| `edge_expansion` | 1.109 | 36 | 141.12 | **127×** |
| `integer_factorization` | 9.763 | 6 | 166.91 | **17.1×** |
| `discrete_log` | 1.542 | 1 | 14.52 | **9.4×** |
| `kcenters` | 16.434 | 8 | 122.07 | **7.4×** |
| `convex_hull` | 4.321 | 6 | 6.84 | 1.6× |
| `rbf_interpolation` | 1.058 | 1 | 0.00 | — see below |

Two of those need the same treatment §57 gave `edge_expansion`: **do not pool across the thing that
changes the outcome.** Here it is the budget.

### 60.1 `convex_hull` is not "loses at $1, wins at $3"

| budget | n | runs | median | vs arm A |
|---|---|---|---|---|
| $1 | 3 | 2.60, 2.03, **11.08** | 2.595 | 0.60× |
| $3 | 2 | 2.55, **12.18** | 7.363 | 1.7× |
| $10 | 1 | 26.65 | 26.65 | 6.2× |

**§48.1's headline — "a 2.8× win over arm A at $3" — rested on dsCH3 alone**, and ds3Hull at the
same $3 scored 2.55. Meanwhile dsHull at **$1** scored 11.08, beating arm A by 2.6× on the budget
where §48.1 says we lose. The task is bimodal the same way `edge_expansion` is, and the budget
explains less of it than that section claimed. What survives is the trend across budgets — medians
2.60 → 7.36 → 26.65 — on n = 3, 2, 1, which is a direction and not a result.

### 60.2 `rbf_interpolation`'s 0.00 is not a loss, it is a NO-MEASUREMENT

The single `rbf_interpolation` probe is dsRBF, and §47 already recorded what happened to it: the
`novelty_mode=llm` gate spent 2,254 s on a proposal it then rejected, the budget ran out, and the run
finished with ONE node whose metric was 0.0 — a solver that raised `LinAlgError` three times of
three. **No number was measured on that task.** Filing 0.00 as "a loss to arm A's 1.058" states a
comparison that never took place, which is the same class of error as §44's `kcenters` row: a cell
filled from the wrong source and then read as a result.

`rbf_interpolation` is UNMEASURED for us, and dsPde — running now on `pde_heat1d` — is the first
probe on any of the three tasks docs/53 §9 lists for unexplained aborts.

**The pattern in all three corrections.** Every one came from reading a table cell instead of the
runs behind it: `kcenters` took the campaign's arm-B figure for our capability, `convex_hull` took
one run for a budget arm, `rbf_interpolation` took a no-measurement for a measurement. The audit that
found them is four lines of Python over `model-probes/*/final.json`, and it should be run against
this document's claims whenever one of them is about to be repeated.

## 61. The budget buys DRAWS, not refinement — and no run has ever converged

Four measurements, none of which this document had made, and together they explain most of what it
has been failing to detect.

**1. Every finished run ends on the ceiling.** `run_finished.reason` across all 46 completed probes:
**`budget_exhausted`, 46 of 46.** Not one stopped because the search was done, ran out of ideas, or
hit a node cap. There is no convergence in this corpus to observe.

**2. The champion arrives at the wall.** Fraction of the budget spent when the best-scoring node
appeared, over the 57 runs with at least two evaluated nodes: median **73 %**, and in **25 of 57
(44 %) it is the LAST QUARTER**. The search is still improving when the money stops.

**3. A dollar buys three nodes.**

| budget | runs | evaluated nodes (median) | range |
|---|---|---|---|
| $1 | 26 | **3.0** | 1 – 3 |
| $3 | 2 | 5.5 | 5 – 6 |
| $10 | 2 | 12.5 | — |

One evaluated node costs a median **$0.339**.

**4. Each node is a fresh draw, not a refinement** — §58: 77 % of node-to-node transitions rewrite
the kernel from scratch, 5 % inherit it.

Put together: **a $1 run is the maximum of about three draws from a wide distribution, and $3 is the
maximum of about five and a half.** That is why §45's $3 `integer_factorization` run landed inside
the $1 range, why §48.2 had to withdraw "three dollars bought less than one", and why every switch
A/B at n = 3 has come back at p ≈ 0.1 and then collapsed: the thing being compared is the max of
three heavy-tailed draws, and one draw's mode assignment (§57) outweighs any switch.

**What this does NOT say.** It does not say more budget is useless — measurement 2 says the opposite,
the champion is still arriving at the wall, so the draws have not stopped paying. It says the RETURN
is the return of extra draws from an unchanged distribution, which grows like the maximum of a
sample and not like refinement.

**The direction it points**, and this is an architecture note rather than a fix: the cheapest way to
raise the expected result is not more draws but making a draw depend on the last one. §58 measured
that it does not — a working kernel survives to the next node once in twenty — and §52.2 explains
why: every node is an independent submission by construction, which is the same property that makes
`node_repaired` fire zero times and `revert` unnecessary. Changing it is a decision about the loop,
not a sweep-sized repair, and it is recorded here with the numbers that would justify taking it.

## 62. The kernel bit is `edge_expansion`'s law, not the loop's

dsCH4 — `convex_hull` at $3, **$3.029, five nodes, test 18.1934**, champion node_4 (18.0957) with
**no kernel at all**. The one node that shipped a `.pyx` — node_1, `qhull.pyx` — scored **8.6787,
the lowest of the five**. 432 min wall, LLM occupied 368 min (85 %), evaluation 7.4 min; money
`plan_step` $1.331 / 287 calls, `propose` $0.575 / 132.

§57 and §61 lean on "the kernel bit decides a run". Re-derived per task, that is `edge_expansion`'s
law and not the loop's:

| task | champion has a kernel | champion has none | ratio |
|---|---|---|---|
| `edge_expansion` | **212.86** (n = 21) | 27.80 (n = 15) | **7.7×** |
| `integer_factorization` | 178.76 (n = 5) | 79.58 (n = 1) | 2.2× |
| `convex_hull` | 12.18 (n = 1) | **6.84** (n = 6) | 1.8×, and the two BEST results — solHull 26.65 and dsCH4 18.19 — carry **no kernel** |
| `kcenters` | — | **all 8** | Cython is never written there at all |

So on `kcenters` the bit does not exist (§33 already said why: the ceiling comes from numba), on
`convex_hull` the best runs do not use it, and on `integer_factorization` it is worth 2.2× rather
than 8×. **Every generalisation §50 → §57 → §61 made about "the bit" was derived from
`edge_expansion` alone**, which supplies 36 of the corpus's runs and is the task those sections were
written over. The mechanism is real where it was measured and is not a property of LoopLab.

### 62.1 `convex_hull` by budget, now n = 3 vs 3

| budget | runs | median | vs arm A (4.321) |
|---|---|---|---|
| $1 | 2.03, 2.60, 11.08 | 2.60 | 0.60× |
| $3 | 2.55, 12.18, **18.19** | **12.18** | **2.8×** |

Rank sum 13 of a possible 15, exact one-sided **p = 4/20 = 0.200**. So $3 beats $1 by 4.7× at the
median and the test still does not clear — the $1 arm's 11.08 and the $3 arm's 2.55 overlap, which is
§60.1's point restated with one more run on each side. What §48.1 claimed at n = 1 ("a 2.8× win at
$3") now holds AT THE MEDIAN of three, which is the first time that sentence has had a sample behind
it.

**dsCH5 launched** — `convex_hull` at $1, taking the budget comparison to n = 4 vs 3, where §57's
arithmetic puts the exact floor at 1/35 = 0.029 and the question becomes answerable rather than
suggestive.

## 63. dsKc2, and a ledger of which claims actually have a sample

dsKc2 — `kcenters`, $1.013, two nodes 105.2705 → **192.3320**, champion node_1, **test 178.5906**.
No kernel: the champion is 304 lines of `numba` + `numpy`, which is what every `kcenters` champion in
the corpus is (§62: 0 of 9 use Cython). 91 min wall, LLM occupied 67 min (73 %), and `deep_research`
took only $0.064 / 25 calls — the smallest research share of any run this week.

`kcenters` post-repair is now n = 2: dsFixKc 159.51 and dsKc2 178.59, median 169.1, **10.3× arm A's
16.434**. The family no longer rests on seven pre-repair numbers, which is what §59 said it should
stop doing.

### 63.1 What each claim in this document is actually standing on

§60's audit corrected three rows by reading the runs instead of the cells. The obvious follow-up is
to write down, once, how much sample each surviving claim has:

| task | probes | claim | standing |
|---|---|---|---|
| `edge_expansion` | 36 | 127× arm A | solid, and the source of every over-generalisation §62 had to walk back |
| `kcenters` | 9 (2 post-repair) | 7.4× at the median, 10.3× post-repair | solid |
| `convex_hull` | 7 | 2.8× at $3 | n = 3 vs 3, p = 0.200 — a median, not a result |
| `integer_factorization` | 6 | 17.1× | solid |
| `discrete_log` | **1** | 9.4× | **one run** |
| `pde_heat1d` | 0 finished | — | dsPde is the first |
| `rbf_interpolation` | 1, unscorable | — | §60.2: a no-measurement, not a loss |

**`discrete_log`'s 9.4× has been quoted in three sections and rests on a single probe** whose first
node scored 0.0 in the 504-second timeout cascade of §40.2. That is the weakest load-bearing number
in the document, and it is weak in the direction that flatters us.

**dsDL2 launched** on the freed lane — `discrete_log` at $1, the second probe on that task, against
dsDL's 14.5186, arm A's 1.542 and arm B's 1.211. It is chosen over another `convex_hull` or
`kcenters` point precisely because it is the thinnest claim rather than the most interesting one.

## 64. dsPde: 124.6× on `pde_heat1d`, and a checker that punishes being right

The first probe ever finished on `pde_heat1d`. It is also the sharpest single result in the corpus
after `edge_expansion`, and the reason is not speed — it is what the loop discovered about the
grader.

| | arm A | arm B | dsPde |
|---|---|---|---|
| `pde_heat1d` final | 1.10095 | 2.450 | **124.631** (test, 100 % valid) |

Cost $1.0060, ledger and `events.jsonl` agree to the cent. 267 generations, 128 min wall, of which
the LLM was occupied **117 min = 92 %** — the highest occupancy measured in this corpus (dsKc2 was
73 %, dsIF5 68 %). On this task local evaluation is nearly free, so the budget is essentially a
pure token budget and the loop is never waiting on the machine.

**Three nodes, two evaluated.** node_0 scored 75.507, node_1 scored 121.9259 and became the
champion (confirmed with `extract_champion`, not file times); node_2 was created with an empty
`files` map and never evaluated — `budget_exhausted`, the forty-seventh consecutive run to end that
way (§61).

**Where the dollar went, by `attributes.phase`:**

| phase | $ | calls | share |
|---|---|---|---|
| `plan_step` | 0.3035 | 81 | 30 % |
| `plan` | 0.2271 | 52 | 23 % |
| `propose` | 0.1981 | 46 | 20 % |
| `deep_research` | 0.1564 | 51 | 16 % |
| `repropose` | 0.0791 | 15 | 8 % |
| rest (`foresight_rank`, `hyp_prioritize`, `novelty`, `card_build`, `hypothesis_merge`) | 0.0417 | 22 | 4 % |

53 % on planning against 28 % on proposing. That ratio is the one §61 flagged: the budget buys
draws, and half of each draw is spent deciding what to draw.

**What it proposed and discarded — the finding.** The five hypotheses in order:

1. how much does the closed-form spectral (DST-I) solution beat a numba RK45 port;
2. does the spectral closed form stay inside `is_solution`'s `allclose(rtol=1e-5, atol=1e-8)`;
3. is a numba-jitted direct spectral kernel faster than `scipy.fft`;
4. **can *any* non-step-replicating fast path pass a checker whose `atol=1e-8` is tighter than the
   reference's own truncation error;**
5. what is the per-instance floor of an exact RK45 replication on this box.

The answer to (4) is no, and the champion's own docstring records it: *"the exact closed-form
(DST-I diagonalisation) was tested and REJECTED by the reference's `is_solution()`: the reference's
own RK45 trajectory carries ~4e-7 truncation error, larger than the checker's `atol=1e-8`, so any
solution closer to the true answer than the reference itself fails."* The loop started out
believing the mathematics was the win, measured that the grader forbids it, and pivoted to
**porting scipy's RK45 controller into a single `@njit` function** — `select_initial_step`, the
Dormand-Prince tableau, `SAFETY=0.9`, `MIN_FACTOR=0.2`, `MAX_FACTOR=10`, `min_step=10*ulp` — with
the tridiagonal heat RHS inlined and a pure-numpy port of the identical loop as fallback. 310
lines.

**Against the foreign champions.** Of the eighteen held-out solvers for this task, sixteen keep
`solve_ivp` and merely accelerate its right-hand side, in 35–63 lines. Exactly two replace the
integrator outright: GPT-5.4 (253 lines, numba, no `solve_ivp`) and ours (310 lines, numba, no
`solve_ivp`). So the 124× is not a better numerical method — it is the *same* method, compiled,
and the population splits cleanly on whether the model realised the checker demanded that.

Arm A's 1.10095 is the same model on the same task with the arena's own agent, and it is on the
`solve_ivp`-plus-numba side of that split. This is the widest arm-A/probe gap in the corpus after
`edge_expansion`, and unlike that one it does not come from a Cython kernel.

**Caveat, stated because §63.1 exists.** n=1. `pde_heat1d` now carries exactly the standing
`discrete_log` had before dsDL2 — one probe, one number. **dsPde2 launched** on the freed lane
(22-32,70-80, $1, 8803) for the second point, for the same stated reason: the thinnest claim
first, not the most interesting one.

## 65. dsIF6: 205.8× on `integer_factorization`, 61 % of a $3 budget spent on planning

Six nodes, five evaluated, stopped by the ceiling it was given (`Refused: LLM spend ceiling
reached: $3.0143 of the $3.0000`, rc=2 — the honest stop, not a crash).

| node | train metric | files |
|---|---|---|
| 0 | 120.1701 | `rho.pyx` + `setup.py` + `solver.py` |
| 1 | 134.6821 | `factor64.pyx` + `setup.py` + `solver.py` |
| 2 | 198.0993 | `squfof.pyx` + `setup.py` + `solver.py` |
| 3 | 135.2720 | `cyfactor.pyx` + `setup.py` + `solver.py` |
| **4** | **287.8095** | `factor64.pyx` + `setup.py` + `solver.py` — champion (`extract_champion`) |
| 5 | 0.0 | `solver.py` alone; `eval_seconds` 149.9, a real failed evaluation |

**Test split: 205.8223**, built (`build_ext ok`) from `factor64.pyx` + `setup.py`. Note the shape:
node_4 is a *rewrite of node_1's own kernel*, the only place in this run where a later node builds
on an earlier one rather than starting over — and it is the champion.

**The task now has seven probes**, the second-best-attested after `edge_expansion`:

| probe | test | kernel |
|---|---|---|
| dsIF | 96.2102 | no |
| dsIF2 | 178.7585 | no |
| dsIF3 | 194.8199 | no |
| dsIF3x | 155.0593 | no |
| dsIF4 | 79.5759 | no |
| dsIF5 | 286.5205 | **yes** |
| dsIF6 | 205.8223 | **yes** |

Median 178.76 against arm A's **9.76319** — 18.3×, and §63.1's "17.1×" is superseded. The two
Cython probes are ranked 1 and 2, but 2 against 5 cannot carry that: the smallest attainable
one-sided p is 2/21 ≈ 0.095, so this is a *pattern worth a third kernel point*, not a result. Said
plainly because §62 had to walk back exactly this inference once already.

**Against the foreign champions.** Of sixteen held-out solvers, eleven call `sympy.factorint`,
four hand-roll Pollard/Brent in Python, one names cython. None ships a compiled 64-bit kernel with
a build recipe. Our top two do, and they are the two largest numbers on this task.

**Where the $3 went.** 737 generations, 6.56 h wall, LLM occupied 5.47 h = **83 %**.

| phase | $ | calls |
|---|---|---|
| `plan_step` | **1.8316** | 429 |
| `deep_research` | 0.4479 | 111 |
| `plan` | 0.4132 | 77 |
| `propose` | 0.2452 | 62 |
| rest (11 phases) | 0.0773 | 58 |

**61 % of this $3 budget went to `plan_step`, and it bought five evaluated nodes.**

*Correction, measured the same day.* The sentence that stood here — "tripling the budget did not
triple the draws, it lengthened the deliberation" — does not survive its own corpus. Planning share
(`plan`+`plan_step`) over all 68 runs with more than $0.30 of spend:

| budget | n | median planning share |
|---|---|---|
| < $1.50 | 57 | 56.6 % |
| ≥ $2.50 | 10 | 57.4 % |

Indistinguishable. dsIF6's 74.4 % and dsPde's 52.7 % are both ordinary draws from a spread that
runs 0 % to 79 %, and the two $10 runs are the LOW outliers (29.1 %, 28.8 %) — the opposite of what
I claimed. The true statement is stronger and more general than the one it replaces: **the loop
spends about 57 % of whatever it is given on planning, and the budget does not move that dial.**
Whether those 57 % buy anything is the open question; it is not answered by comparing two runs.

**Six hypotheses, all empirical**, and one of them is the run: *"Does SQUFOF's 1.47× edge over
Brent-rho hold across all 100 graded instances, and does a SQUFOF-primary + Montgomery-rho fallback
…"* — node_2 is the SQUFOF kernel (198.10), node_4 the Montgomery-rho one (287.81). The loop
proposed both, measured both, and kept the winner.

**Two defects fell out of this probe and are fixed (`f5a5192b`).** All six nodes fired
`critic:no_metric_output` — 34 of 34 corpus-wide, a category error against a library the harness
runs — and reflection's billed calls landed in no span at all. Details in the commit; the second
one means every per-phase table in this document was missing $0.19 of $100.27 it could not see.

**dsRBF2 launched** on the freed lane (33-43,81-91, $1, 8803). `rbf_interpolation` is the thinnest
entry in §63.1 — one probe, and it produced *no number*: dsRBF got exactly one node in 5791 s and
that node raised `LinAlgError: Singular matrix in RBF solve` on 3 of 3 runs. It will be compared
against arm A's 1.05791 and arm B's 1.0466, and it asks a second question the first probe raised
and could not answer: why one node in an hour and a half.

## 66. dsCH5: `convex_hull` at $1 lands at 1.98, and the $1-vs-$3 gap is now testable

Champion node_0 at metric 1.9978, **test 1.9841**, one file, no kernel; stopped by the ceiling
($1.0098 of $1.0000, rc=2). 2 h 05 m wall, 215 calls, planning share 72.4 %.

With it the task has eight probes and the split §63.1 called "a median, not a result" is closer to
being one:

| budget | probes | test speedups | median |
|---|---|---|---|
| $1 | 4 | 11.0803, 2.5955, 2.0313, **1.9841** | 2.31 |
| $3 | 3 | 18.1934, 12.1764, 2.5492 | 12.18 |
| $10 | 1 | 26.6535 | — |

Arm A 4.32122, arm B 1.0892. Rank-summing the seven $1/$3 points gives U = 10 of 12, one-sided
p ≈ 0.11 — better than the p = 0.200 §63.1 recorded, still not a decision. **dsCH6 launched** on
the lane dsCH5 freed (0-10,48-58, **$3**, 8803) for the fourth $3 point: at 4 against 4 a clean
separation reaches p ≈ 0.014, so this is the one probe that can settle the campaign's weakest
comparison rather than merely thicken it. It is a $3 probe deliberately — the $1 side already has
four points and the $3 side three.

## 67. Two commands the arena's agent has, one door we cannot open, and one we forgot to mention

No probe finished this sweep, so this is item 8 alone: a capability comparison against
`AlgoTuner`, done by counting what its agent actually typed rather than by reading its prompt.

**What it uses.** Commands issued by arm A's agent across all twenty task-arms:

| command | calls |
|---|---|
| `edit` | 678 |
| `eval` | 177 |
| **`reference <input>`** | **119** |
| `revert` | 102 |
| **`eval_input <input>`** | **97** |
| `delete` | 26 |
| `profile` | 11 |
| `view_file` | 8 |
| `ls` | 3 |
| `profile_lines` | 0 |

`reference` and `eval_input` together are used more than `eval`. They are the two commands that
answer a question the model INVENTS: *"what does the reference return for this input, and what does
mine return."* The transcript shows the use verbatim — `reference {"composite": 15}`, issued to
"understand the problem sizes". We have neither.

**The door that cannot open.** The obvious repair is an `--input` flag on `looplab_check.py`, and I
wrote it and probed it before checking whether it could be reached: it answered `{"composite": 15}`
with `{p: 3, q: 5}` in 0.1 ms, and refused an invented non-semiprime with the reference's own
`ValueError: Expected 2 factors, but got 3` — exactly the diagnostic value the arena gets. Then the
measurement that mattered: `run_dev_command(name)` takes a NAME and no arguments, deliberately, so
the operator's argv cannot be forged ("you cannot invent a command or its arguments"). The model
could never call it. **Reverted rather than shipped as an unused door.**

**The affordance that was missing.** `reference_<task>.py` is staged in the workspace, and
`run_probe` runs Python over that tree — so the capability has been there the entire campaign. What
was missing is anyone saying so:

| over the whole probe corpus | count | share |
|---|---|---|
| `run_probe` calls | 3,124 | — |
| …that import the reference at all | 95 | **3.0 %** |
| …that call `is_solution` or `generate_problem` | 72 | **2.3 %** |

Against 216 uses in 20 arena task-arms. The models were hand-writing timing loops next to a module
that answers the question — which is the same shape as the `check` command's own finding (§ its
comment: "its model never writes a validation harness by hand; ours was handed a blank `run_probe`
and wrote timing loops with it"), one rung further up.

The card now names the module and all three methods (`b4f4feba`). **Acceptance is by probe, not by
the commit**: the next probe launched carries it, and the number to beat is 3.0 % / 2.3 %.

**Two other leads, both closed by measurement rather than fixed.**

* *"`eval_train` gives only aggregate speedup"* (dsIF5, verbatim). Checked what the arena's agent
  actually receives from `eval`: `Speedup: 1.08x / Valid Solutions: 100% / Invalid: 0% / Timeouts:
  0%`. The same aggregate. Not a gap.
* *Models abandoning Cython.* The pip repair removed its stated cause completely — "No module named
  pip" appears in 92 generations before 2026-08-28 08:49 and **0** after. But abandonment did not
  fall (3.26 per 1000 generations before, 4.05 after), and reading the sixteen post-repair passages
  shows why: they are the model WEIGHING Cython against numpy on measured grounds, which is the
  reasoning we want. No defect.

**And my own card fix from this morning, verified by probe.** `2d3c2d43` added the sentence telling
a Researcher that the developer commands are not on its tool surface. Splitting proposer-role
generations that name a developer command at the commit boundary:

| | before | after |
|---|---|---|
| name a developer command | 743 | 83 |
| …and say they have no such tool | **45 (6.1 %)** | **0 (0.0 %)** |
| …and say "I cannot run code" (TRUE for a proposer) | 131 (17.6 %) | 17 (20.5 %) |

The confusion is gone and the true statement is untouched — the intended effect exactly. I nearly
reported the opposite: a first pass with one regex covering both sentences gave 21.9 % vs 21.0 %
and I wrote "the fix did not work" before separating them. Fourth time this session that a
measurement reversed a conclusion, and the first where the sloppy instrument was the regex itself.

## 68. dsDL2 at 2.84, and the 9.4× claim collapses

`discrete_log`, $1.0041, 131 min, LLM occupied 114 min = **87 %**. Two nodes created, **one
evaluated**: node_0 at 2.5832 became champion by having no rival, and node_1 was created with an
empty `files` map when the money ran out. **Test 2.8369.**

| | arm A | arm B | dsDL | **dsDL2** |
|---|---|---|---|---|
| `discrete_log` | 1.542 | 1.211 | 14.5186 | **2.8369** |

§63.1 flagged this as the campaign's thinnest claim — "9.4×, one run" — and the second run says why
that flag was right. **The two probes differ by 5.1×, and the mechanism is visible: dsDL got a
SECOND draw and dsDL2 did not.** dsDL's node_0 scored 0.0 (the timeout cascade) and its node_1
scored 14.5385; dsDL2 never reached a node_1. Median of two is 8.68 and it means very little.

Both champions are the same family — `sympy` + Pohlig-Hellman + BSGS with a numba inner loop, 313
lines for dsDL2. Of seventeen foreign champions, twelve are 7–86 line `sympy.discrete_log`
wrappers and five hand-roll BSGS at 257–411 lines; ours sits with the second group.

Phases: `plan_step` $0.4033 (40 %), `propose` $0.1822, `plan` $0.1752, `deep_research` $0.1573.
Five hypotheses, all sharp — including one that reads as a direct answer to §66's question about
where a run's time really goes: *"Does the scorer's process lifecycle (module import inside the
measured time, forkserver 1-CPU-thread isolation, per-instance …)"*.

### 68.1 What a dollar actually buys, measured properly this time

§65's withdrawn claim was about the planning SHARE, which does not move with budget. This is the
number that does:

| | runs | evaluated nodes (median) | mean | $ per evaluated node |
|---|---|---|---|---|
| ~$1 probes | 55 | 2 | 2.47 | **$0.409** |
| ~$3 probes | 8 | 5.5 | 5.38 | **$0.560** |

Tripling the budget buys 2.2× the draws, and each draw costs **37 % more** at $3 than at $1. Seven
$1 runs got one draw or none: dsCH, dsCH5, dsDL2, dsFBKc2, dsRBF, fxSpectral, opus5.

And the tail is paid for: **$3.6067 of $100.2691 (3.6 %) is spent after the last evaluated node**,
on a draw the run never finishes. 16 of 69 runs end holding one, 11 of them with no files at all.
dsDL2 spent 30 % of its budget that way.

That is the defect fixed this sweep (`812b147a`): `propose`, `repropose`, `plan`, `foresight_rank`
and `hyp_prioritize` have **never once** seen the money — 0 of 8,339 resolved prompts — while
`plan_step` sees it in 72.8 % of 8,298. The role that implements is told the budget; the roles that
decide what to build are not. Acceptance is by probe.

**dsDL3 launched** on the freed lane (11-21,59-69, $1, 8803): the third `discrete_log` point
against 14.5186 and 2.8369, and the first probe to carry the §67 card clause — its acceptance
numbers are 3.0 % of probes importing the reference and 2.3 % calling `is_solution`.

## 69. Three leads on long generations, and why none of them became a fix

No probe finished this sweep. Item 8 only — and this one ends in nothing shipped, which is the
report, not an absence of one.

**The lead.** dsDL3 sat 815 s without a metered call while its event log kept ticking. Not a hang:
`wchan=do_epoll_wait`, four sockets, a stream in flight. The call before it was **16,186 prompt
tokens and 66,943 completion tokens** — one generation. dsDL3 is the first probe carrying the §67
card clause, so the first suspicion was my own change.

**Refuted.** Over the 13,267 metered calls: median completion is **524** tokens, p90 10,757,
p99 32,092, **max 254,180** (dsIF4, 05:25, long before the clause). 160 calls sit at ≥30k. dsDL2 —
no clause — produced 64,278 in one call the same afternoon. dsDL3's 66,943 is the ninth largest and
squarely inside the existing distribution.

**What those 160 calls cost.** 1.2 % of calls carry **15.0 % of all completion tokens** and 5.1 % of
spend ($2.43 of $48.05 on port 8803). That is a large enough slice to be worth a rule — so I looked
for one.

**The finding that shrank.** Holding money equal (the 33 probes in the $1 cohort), the share of a
run's completion tokens sitting in ≥30k calls against the number of nodes it evaluates:

* Spearman **ρ = −0.470**, permutation one-sided **p = 0.0028**.
* Heavy runs (≥20 % of completion in giant calls, n=7): median **2** evaluated nodes.
* Light runs (<5 %, n=12): median **3**.

Then the control. WITHIN a task the effect nearly vanishes: `edge_expansion` (n=17) is flat at 3
nodes from 0 % to 14 % and only dips to 2 at the extremes, `convex_hull` (n=3) is 1 node at 7 %,
27 % and 38 % alike, `integer_factorization` (n=5) is mixed. Concordant pairs 37, discordant 15 —
a direction, not a law. **The cross-task correlation is mostly task difficulty**: a hard task both
provokes long reasoning and yields few nodes.

So no `max_tokens` cap ships today. Choosing a ceiling mid-campaign off a confounded correlation is
the same instrument error `DELTA_CEILING_DEFAULT = 0` exists to prevent. The honest next step is an
experiment, not a patch: two probes on ONE task at $1, identical but for a completion cap.

**The third lead, closed by reading.** All ten "cut a streaming response mid-body" events in the
corpus kept text that was **100 % reasoning and 0 % content** — 12,906 to 502,165 characters, never
a single content character. It reads like a rescue that rescued nothing, and I was about to make
the notice say so. `salvaged_lengths`' own docstring stopped that: reporting the reasoning-only case
as "0 characters kept" WAS the previous bug, found on live fire and deliberately repaired, because
"a reasoning model cut by a gateway mid-think has spent everything on `reasoning_content` and has
not begun its answer" is the normal shape here. The notice is right as written. Checked the
aftermath too: the runs recover — the five calls after each cut are ordinary 100–30k ones, no retry
storm.

### 69.1 An acceptance criterion, pinned before its data arrives

§67's card clause is measured by whether probes query the reference. dsDL3 carries it and has made
**zero** `run_probe` calls in 28 minutes — it is still inside that first 67k-token draft, so there
is nothing to accept or reject yet.

And the baseline I named in §67 is the wrong one. 3.0 % / 2.3 % is the LIFETIME corpus figure; the
three probes running right now on the old card sit well above it:

| probe | card | `run_probe` | imports reference | calls `is_solution`/`generate_problem` |
|---|---|---|---|---|
| dsCH6 | old | 37 | 5.4 % | 8.1 % |
| dsRBF2 | old | 24 | 8.3 % | 8.3 % |
| dsPde2 | old | 41 | 4.9 % | 4.9 % |
| **dsDL3** | **new** | **0** | — | — |

**The comparison that counts is dsDL3 against 4.9–8.3 %, not against 3.0 %.** Written down now,
before the numbers exist, so the goalposts cannot move later.

## 70. dsPde2 reproduces the 124×; dsRBF2 is the first task we simply do not win

Two probes finished, both at $1, both with **one evaluated node and an empty node_1** — the shape
§68 measured and `812b147a` was written for. Neither carried that fix (both launched before 18:09).

### dsPde2 — `pde_heat1d`, test **99.0029**

Champion node_0 at 103.1502, 222 lines, 135 min wall with the LLM occupied **128 min = 95 %**, the
highest occupancy in the corpus. Phases: `plan_step` $0.2941, `propose` $0.2389, `plan` $0.2386,
`repropose` $0.1022, `deep_research` $0.0960.

| `pde_heat1d` | arm A | arm B | dsPde | dsPde2 |
|---|---|---|---|---|
| test | 1.10095 | 2.450 | 124.631 | **99.0029** |

**The important part is not the number, it is that the finding reproduced.** §64 reported that
dsPde discovered the checker forbids being more accurate than the reference. dsPde2, a fresh run,
wrote it again in its own words without prompting: *"is_solution() accepts a result only if it is
allclose(rtol=1e-5, atol=1e-8) to the reference's OWN RK45 trajectory. The reference's integration
error (~4e-7) is larger than that tolerance, so an exact/analytic solution of the ODE FAILS the
checker."* Its first hypothesis is the same question — *"How much numerical slack does is_solution
actually leave"* — and its champion is again a numba port of scipy's RK45 controller. **Two probes,
two independent rediscoveries, 2 of 2.** Where `discrete_log` fell apart on its second point (§68),
this one held: median 111.8 against arm A's 1.10.

### dsRBF2 — `rbf_interpolation`, test **0.9977**

Champion node_0 at 1.0127, 62 lines, 96 min, LLM 73 %. Phases: `plan_step` $0.4713, `propose`
$0.2567, `deep_research` $0.1510.

| `rbf_interpolation` | arm A | arm B | dsRBF | dsRBF2 |
|---|---|---|---|---|
| test | 1.05791 | 1.0466 | no measurement | **0.9977** |

§63.1 called dsRBF "a no-measurement, not a loss". It is now measured, and it is a loss — 0.3 %
BELOW the reference. The structural reason is visible in the held-out set: **all seventeen foreign
champions call `scipy`'s `RBFInterpolator`, the same library the reference calls**, in 17 to 239
lines. There is no algorithm to beat, only scipy calling itself, and the best anyone manages is a
few per cent of dispatch overhead. Our champion is the same shape — `RBFInterpolator` with an
adaptive local/global switch at n=800, `neighbors=128` above it — and the switch does not pay.

This is the first task in the probe corpus where LoopLab produces nothing over the baseline, and
saying so is the point: the 127× on `edge_expansion` and the 111× median here are not a general
claim about the loop, they are claims about tasks where a compiled inner loop or a replicated
integrator exists to be found.

### 70.1 Two more data points for the unfinished draw

dsPde2 spent **$0.3913 of $1.0042 (39 %)** after its last evaluated node; dsRBF2 **$0.2515 (25 %)**.
With dsDL2's 30 % that is three consecutive $1 probes losing a quarter to two fifths of the budget
to a draw the run cannot finish — the corpus figure in §68 was 3.6 % averaged over 69 runs, and the
recent single-node runs are far worse than that average.

**dsPde3 and dsEE launched** on the two freed lanes (22-32,70-80 and 33-43,81-91, $1 each, 8803).
Both carry the §67 card clause AND the §68 money cue, so they are the acceptance probes for both.
`pde_heat1d` because it has now ended with an unevaluated node twice running and its two points
(124.63, 99.00) give a tight band to detect a change against; `edge_expansion` because with 36
probes it is the corpus's most stable denominator — its $1 runs land on 3 evaluated nodes almost
every time, which is exactly what makes a change in nodes-per-dollar visible. Neither had reached a
`propose` generation at the time of writing, so the cue's own acceptance measurement (does
"Spend guidance" appear in the resolved prompt) is still pending, not passed.

## 71. Measurements per dollar: 11.5 against 35, and the arithmetic I had to correct first

Item 8 this sweep is a measurement, not a patch, and the first version of it was wrong.

The arena's agent and our loop ran the same model on the same gateway at the same **$1.00 per
task-arm** (`rerun_arm_a.sh`: `BUDGET_USD=1.00`). Counting the evaluations arm A's agent actually
RECEIVED, per task-arm:

| task | arm A scored evaluations |
|---|---|
| `kcenters` | 58 |
| `spectral_clustering` | 60 |
| `convex_hull` | 57 |
| `multi_dim_knapsack` | 54 |
| `integer_factorization` | 49 |
| `pagerank` | 47 |
| `rbf_interpolation` | 35 |
| `pde_heat1d` | 25 |
| `discrete_log` | 15 |
| `edge_expansion` | 14 |
| `min_dominating_set` | 7 |

Median **35**. Against our $1 probes' median of **2 evaluated nodes**, that reads as 17×, and that
is the comparison I nearly wrote down. It is the wrong one: our nodes are not our measurements.
`run_dev_command("eval_train")` is, and over the corpus the Developer calls it **1,027 times across
68 runs — a median of 11.5 per run, up to 53**. (`check` 612, `profile` 287.)

So the honest figures are **11.5 measurements per dollar against 35, a factor of 3** — and then a
second gap underneath it: of our 11.5 measurements, **2 become nodes**. The arena's `eval` IS the
state update; every one of its 35 measurements directly moves the best-known solution. Ours has an
extra step — measurement, then a propose/plan/build cycle to turn a measurement into a node — and
that is where the throughput goes, not in reluctance to measure. The card's "profile freely,
measure rarely" advice is not the culprit either: 11.5 calls at ~40 s each is not rarely.

**Nothing shipped today, deliberately.** Two changes from the last two sweeps — the §67 card clause
and the §68 money cue — are both still awaiting probe acceptance, and dsPde3/dsEE are the first
runs carrying either. Landing a third unverified change on top of them would make all three
unattributable, which is the same mistake as measuring a card fix with a pattern that matches the
card fix. The next thing to ship is whichever of those two the probes say worked.

## 72. Three probes on a verified ruler, and the one metric that was looking the wrong way

The first numbers this programme has produced whose ruler was checked the same day, on this box,
by the standing rule: a solver delegating to the reference must score ~1.0. Measured before the
probes ran — `pagerank` 1.0024 / 1.0022 / 0.9997, `pde_heat1d` 0.9958, `edge_expansion` 0.9847,
`discrete_log` 1.0162 — against baselines re-measured here, because the cache restored from the
2026-08-29 snapshot proved to be from a machine that timed the reference 6.4 % faster.

| probe | task | nodes (train) | TEST | champion carries |
|---|---|---|---|---|
| `remEE` | `edge_expansion` | 132.69 → 183.60 | **179.6451** | `.pyx` + `setup.py` |
| `remDL2` | `discrete_log` | **14.29** → 13.98 | **14.0483** | `.pyx` + `setup.py` |
| `remPde` | `pde_heat1d` | 54.26 | **54.1227** | 4 `@njit` kernels (*corrected 2026-09-01; this row said "plain Python, no kernel"* — see §73.2) |

### 72.1 `discrete_log` was not a 5.1× spread; it was one low run

§68 read dsDL 14.5186 against dsDL2 2.8369 and called the difference 5.1×, the widest in the
corpus, and §63.1 had already flagged 9.4× as "the weakest load-bearing number in the document".
The third point is **14.0483**, four percent from the first. Two of three cluster at 14; the 2.84 is
the outlier, and the mechanism §68 named for it stands — dsDL2's second node was created with an
empty file map when the money ran out, so it never got the second evaluated draw the other two had.

This does not restore 9.4×. It says the task is reproducible when the run reaches a second draw,
and that a single low run had been carrying an interval nobody could re-derive.

### 72.2 The champion is the best EVALUATED node, demonstrated rather than read

`remDL2` produced node_0 at 14.2947 and node_1 at **13.9819** — the later draw was worse — and
`extract_champion` returned node_0. Until now this guarantee was established only by reading
`state.best()`; no surviving run had a later-worse node to show it. It also sharpens why the card
should say so: the engine already protects a late risky attempt from costing what was earned, and
the model is never told.

### 72.3 The waste metric was pointed at the wrong end of the run

Three sections have tracked money spent AFTER the last evaluated node — 25–39 % across dsDL2,
dsPde2 and dsRBF2, "the draw the run cannot finish". Measured on these three: `remEE` 36 %,
`remPde` **11 %**, `remDL2` **1 %**. By that metric `remPde` is the healthiest of the three.

It is the worst. `remPde` spent **$0.74 of $1.0050 before its first node existed** — 103 `plan_step`
generations, 61 % of the budget, against 34 for `propose` — and the single node it did build was the
only one it could afford. Its 54.12 was the lowest `pde_heat1d` had scored across four probes
(124.63, 99.00, 121.85, 54.12).

*Corrected 2026-09-01.* The sentence that stood here — that the three high runs shipped a numba
kernel and this one shipped plain Python, never getting as far as compiling — is false. `remPde`'s
champion carries four `@njit` kernels; its `from numba import njit` sits indented inside a `try:` at
line 80 behind a `_HAVE_NUMBA` flag, and the grep that produced the claim never saw it. numba 0.67.0
is installed in the venv these are scored in, so the kernel ran. Every `pde_heat1d` champion on this
box carries one, across a 0.0-to-129.75 spread (§73.2), so the artefact difference this sentence
appealed to does not exist. The spend figures below are unaffected — they were computed, not read
off the file.

So "spend after the last node" measures a tail that a run reaching its ceiling mid-draw will always
have, and misses entirely a run that spends its budget before the first draw. The pair to watch is
**spend before the first evaluated node** beside it. On these three: 74 %, and two runs under 20 %.

### 72.4 What the money cue reaches, on three runs in a row

`propose` and `repropose` carry the Spend guidance line; `plan`, `plan_step`, `deep_research`,
`foresight_rank` and `hyp_prioritize` do not — the reach is pinned in
a CLAIM pin in `proposal_cues.py` with the slug `llm-budget-cue-reaches-propose-only`. The
cost of the gap now has a
number twice over: `plan_step` + `plan` took **73.7 %** of `remPde`'s dollar and **61.1 %** of
`remDL2`'s, and neither role can see what is left. The role that spends is the role without the
receipt.

### 72.5 The reference module: still zero, now on nine files

Across the three probes, **0 of 9** loop-written files import `reference_<task>` or call
`is_solution` / `generate_problem`, against the corpus base §69.1 corrected to 4.9–8.3 %. One probe
proved nothing; three in a row are worth stating: the card names the module and the model does not
open it. Whether the clause is unread, disbelieved or simply not worth the probe is not established
here.

### 72.6 A measurement that was mine, not the loop's

The first `discrete_log` attempt of the day (`remDL`, abandoned at $0.1292) lost **28 % of its calls**
to 504s at exactly 300 007–300 011 ms. That is nginx's `proxy_read_timeout`, which
`benchmarks/meter/proxy.py` already documents, and it measures the gap BETWEEN BYTES — so it fires
only without streaming. The campaign's ledger shows `stream=True` on 2385 of 2534 `discrete_log`
calls and successful latencies up to 1824 s; today's abandoned run shows `stream=False` on all 40.

The cause was the operator's, not the loop's: `/home/jovyan/data/looplab/.env` line 77 sets
`LOOPLAB_LLM_STREAM=false`, and the box profile publishes the flag as `${LOOPLAB_LLM_STREAM:-1}` —
a default-if-unset, which an already-set value beats silently. Sourcing that file for two
credential lines turned streaming off for every probe launched today until it was noticed.
Relaunched with the flag set explicitly: **0 aborts in 93 streamed calls**.

Anything measured in LLM-time under that regime — nodes per dollar, share of wall on the LLM,
`eval_train` per run — is not comparable across it. Solver speedups are unaffected: the evaluator
runs no model.

## 73. The test split adds no noise, and on `pde_heat1d` the kernel bit does not separate

Six probes have now finished on this box. Their evaluated nodes' train speedups, and the test score
the champion earned afterwards:

| probe | task | evaluated nodes (train) | TEST |
|---|---|---|---|
| accEE | edge_expansion | 27.466, **221.539** | 224.8846 *(re-scored 2026-09-01, see 73.4)* |
| remEE | edge_expansion | 132.695, **183.603** | 179.6451 |
| remEE2 | edge_expansion | 27.833, **101.153** | 102.175 |
| accPde | pde_heat1d | **119.795** | 120.7621 |
| remPde | pde_heat1d | **54.259** | 54.1227 |
| remPde2 | pde_heat1d | **29.562** | 30.3282 |
| remDL2 | discrete_log | **14.295**, 13.982 | 14.0483 |

**Train and test agree on every run**: 221.5→224.4, 183.6→179.6, 101.2→102.2, 119.8→120.8,
54.3→54.1, 29.6→30.3, 14.0→14.0. Seven for seven, worst disagreement 2.6 % (`remPde2`, 29.562 -> 30.3282). Whatever spread this
corpus carries is present BEFORE the champion is scored, and the held-out split contributes none of
it. That is worth having written down: it means a train number can be quoted as a run's result
without waiting for the test pass, and it means no explanation of the spread may appeal to the split.

**The ruler still reads 1.0, today, under load.** Re-verified while three probes were live, against
the same cached `__w22x1r3` baselines the probes used: a solver that IS the reference scored
**0.9795** on `edge_expansion` and **1.0363** on `pde_heat1d`. The reference has to be INLINED for
this — a submitted solver that imports `reference_<task>.py` fails in the graded sandbox with
`solver_unloadable`, which is the mechanical reason behind the card's "do not import from it".

### 73.1 What these three EE points are NOT

They are not a decline through the day, and they are not new evidence about `edge_expansion`'s
spread. §50 measured all 27 `edge_expansion` runs at $1 — range 18.5-344.4, ratio 19×, CV 87 % — and
found the variable that splits them: whether the champion ships a compiled kernel (median 217.3 with,
27.8 without). All three ship one (`remEE`, `remEE2`'s `count_cross.pyx`, and `accEE` by its node metric — its champion was never extracted, see 73.4),
so 221.5 / 183.6 / 101.2 sit unremarkably inside that population's 27.8-344.4. Read in time order
they look monotone, and that reading is available and worthless: three points fall in a monotone
order one time in six.

The pair is still worth keeping for one reason §50's 27 runs cannot supply. `remEE` and `remEE2` ran
a BYTE-IDENTICAL 15,553-character goal card, same task, same $1.00, same verified ruler, four hours
apart, and returned 183.6 and 101.2. *Withdrawn 2026-09-01 (§80): the card was identical and the
INSTRUMENT was not — `remEE` ran unstreamed and lost nine calls to the gateway's 300 s ceiling,
`remEE2` ran streamed and lost none. The pair is not one configuration observed twice, and no floor
follows from it.*

### 73.2 On `pde_heat1d` the kernel bit does not separate, and that is new

§50's one-bit explanation is specific to `edge_expansion`. It does not carry:

*Corrected 2026-09-01, and the correction makes the section simpler.* Every `pde_heat1d` champion
on this box carries a numba kernel — including `remPde`, which this section and §72 before it both
called plain Python:

| probe | champion | kernel | TEST |
|---|---|---|---|
| remPde3 | 237 lines | 2 `@njit` | 129.75 |
| accPde | 209 lines | 7 `@njit` | 120.7621 |
| remPde | 428 lines | 4 `@njit`, numpy fallback | 54.1227 |
| remPde2 | 242 lines | 4 `@njit` | 30.3282 |
| remPde4 | 196 lines | 3 `@njit` | 0.0 |

`remPde`'s import sits inside a `try:` at line 80 with `_HAVE_NUMBA` and a pure-numpy branch "used
only if numba is unavailable" — and numba 0.67.0 is installed in the very venv these are scored in,
so the kernel ran. The earlier reading came from a grep that never saw an indented import, and it
was repeated in §72 as the mechanism behind the 54.12. It is withdrawn.

So the bit is not merely a poor separator here — it is CONSTANT. All five runs ship a kernel and
they span 0.0 to 129.75. On `edge_expansion` the same bit moves the median by a factor of eight
(§50); on `pde_heat1d` it explains nothing at all, because there is nothing for it to vary with. Whatever separates a good `pde_heat1d` run from a bad one, it is not the bit
that separates `edge_expansion` runs. §72 read `remPde`'s missing kernel as the mechanism behind its
54.12; `remPde2` has the kernel and scored 30.33, so that reading does not survive. What replaces it
is not yet measured, and n = 3 will not settle it — the honest next step is more points on this task
before any mechanism is proposed for it.

### 73.4 `accEE` had no test score on this box; it was recovered, and 224.4432 was right

*Added 2026-09-01.* `accEE` is quoted with TEST 224.4432 in the operator brief and in the tables
above. That number appears **nowhere** on this box — not in the probe tree, not in the archive, not
in any log. What `accEE` actually left behind is:

* `run.log`: `stop: PAUSED (node 2) — resumable, NOT finished`, `pause reason: auto-paused: a
  Developer session crashed`. `BEST node 1: metric=221.539`.
* `probe.log`: `could not fold …/runs/edge_expansion/run: ModuleNotFoundError: No module named
  'looplab'`, then `чемпион: НЕТ` and an empty `ИТОГ:`.

So the champion was never extracted and no test pass was ever run. The import failure is the one
`d3d41531` repaired by putting the repo root on `sys.path` — committed at 06:33 on 2026-08-31, two
and a half hours AFTER `accEE` ran (02:17–04:02). The run hit a bug that was fixed the same morning
and its result was never recovered.

**Recovered the same night, and the old figure was right.** The fix is not to resume the run —
`accEE` already spent $1.0042 of its $1.00, so continuing it would break the budget contract that
makes it comparable. What failed was only the step AFTER the money: champion extraction and the test
pass, neither of which costs a cent. Both were re-run on a free lane against the same `__w22x1r3`
baselines:

    champion node 1 (metric=221.5387) -> champion_solver.py (+2 siblings: edge_expansion_cy.pyx, setup.py)
    {"speedup": 224.8846, "eval_seconds": 41.5, "build": "ok"}

So `accEE`'s TEST is **224.8846**, measured here, with its Cython kernel building. The 224.4432 the
brief carries sits 0.2 % away — the figure was sound all along; what was missing was any evidence for
it on this box, and a number one cannot reproduce is not the same thing as a number that is wrong.
The tables above now carry the re-scored value.

The cost of the whole recovery was CPU on an idle lane. It sat undone for twenty hours because
nothing said the score was missing — which is what the new `probes with NO test score` section of
`probe_summary.py` exists to prevent.

### 73.3 The consequence for reading this document

§50 already said no switch comparison on `edge_expansion` is decidable at n ≤ 4, and withdrew a
p-value on its own next measurement. §73 adds that the same caution is owed on `pde_heat1d`, where
the spread is 4.0× over three points and the one explanation that works elsewhere does not work here.
Where this document compares two runs on one task, it is comparing draws from a distribution whose
width is a factor of two to four. The pooled twenty-task arm comparison is untouched by this: it
repeats tasks, not runs.

## 74. Two probes launched 15 seconds apart scored 129.75 and 0.0

`remPde3` and `remPde4` ran `pde_heat1d` on adjacent lanes, from the same 16,947-character goal card,
with the same $1.00, started fifteen seconds apart. Both spent their dollar to the cent ($1.0077 and
$1.0092). Both submitted a champion carrying a numba kernel.

| | remPde3 | remPde4 |
|---|---|---|
| TEST | **129.75** | **0.0** |
| evaluated nodes (train) | 123.1297, 50.0147 | 0.0 |
| `eval_train` calls | **26** | **11** |
| spend before the first node | 55 % | 85 % |
| spend after the last node | 0 % | 15 % |
| `plan_step` share | 48.8 % | 69.6 % |

129.75 is the highest `pde_heat1d` score in this corpus and 0.0 is the only invalid one. §73 put a
floor of about 1.8× under the noise from the `remEE`/`remEE2` pair — since withdrawn, §80, those two
ran on different instruments; this pair, both streamed, says the floor is wherever a run happens to land, because one of the two never produced a valid
solver at all. `remPde4`'s single node failed `is_solution` with max rel err 1.37e+05 — a build that
worked and computed the wrong numbers, §50's failure mode, on its only draw.

**The champion rule bit again.** `remPde3`'s second node scored 50.01 against its first at 123.13,
and the run submitted the first. §72 recorded `remDL2` as the first empirical demonstration that the
best EVALUATED node is kept rather than the last; this is the second, and a much more expensive one
— submitting the last node here would have cost 59 % of the train score (123.1297 -> 50.0147).

### 74.1 A direction, explicitly not a finding

The sharpest difference between the two is how often they measured: 26 `eval_train` calls against
11. Over all nine scored probes on this box:

| probe | task | `eval_train` | nodes | TEST |
|---|---|---|---|---|
| remEE | edge_expansion | 29 | 2 | 179.6451 |
| accEE | edge_expansion | 27 | 2 | 224.8846 |
| remPde3 | pde_heat1d | 26 | 2 | 129.75 |
| remDL2 | discrete_log | 25 | 2 | 14.0483 |
| remPde | pde_heat1d | 22 | 1 | 54.1227 |
| remPde2 | pde_heat1d | 21 | 1 | 30.3282 |
| remEE2 | edge_expansion | 19 | 2 | 102.175 |
| accPde | pde_heat1d | 18 | 1 | 120.7621 |
| remPde4 | pde_heat1d | 11 | 1 | 0.0 |

Pooled, `eval_train` against TEST gives r = +0.62 — and that number should be thrown away, because
the three tasks have different score scales (edge_expansion 102-224, pde_heat1d 0-130, discrete_log
14) and pooling them measures "which task is this" more than anything else. Within task it is
+0.64 on `pde_heat1d` (n = 5) and +0.85 on `edge_expansion` (n = 3), and **neither ordering is
monotone**: `accPde` measured 18 times and scored 120.76, above two runs that measured 21 and 22.
At n = 3 and n = 5, against a per-task spread of 4×, those coefficients carry no weight.

What is not ambiguous is narrower and worth one sentence: the run that measured fewest is the only
one of the nine that submitted an invalid solver. That is one run, and it is a reason to look, not a
result.

The cheap way to test it is not another correlation over the same nine — it is to make the number
move on purpose and see whether the score follows. That changes the card, so it is a change between
arms, and it needs several runs of each before it says anything at all (§73.3).

### 74.2 The next measurement did not support it — `remDL3`, the same night

§74.1 flagged measurement frequency as "a direction, explicitly not a finding", and said the way to
test it was to make the number move rather than to correlate the same nine runs again. The tenth run
arrived first and points the other way.

`remDL3` (discrete_log, $1.0122, TEST **7.5787**) made **27** `eval_train` calls — more than any
other probe in the corpus, including `remPde3`'s 26 — and scored the second-lowest `discrete_log`
number on record. Against `remDL2`'s 25 calls and 14.0483, the task's two points now run *backwards*:
more measuring, half the score.

| | pooled | edge_expansion | pde_heat1d | discrete_log |
|---|---|---|---|---|
| r (9 runs, §74.1) | +0.62 | +0.85 (n=3) | +0.64 (n=5) | — |
| r (10 runs, with remDL3) | **+0.45** | +0.85 (n=3) | +0.64 (n=5) | 2 points, reversed |

One new observation moved the pooled coefficient by 0.17. A statistic that swings that far on its
tenth sample was never carrying an argument, and §74.1's own sentence — that at this n those numbers
carry no weight — is the part that survived.

The one clause that has not yet been contradicted is still the narrow one: `remPde4`, the run with
the fewest calls, remains the only probe of ten to submit an invalid solver.

`remDL3` also fits §72's other reading of this task better than frequency does. It produced ONE
evaluated node (7.5215 train, 7.5787 test); `remDL2` produced two (14.295, 13.982) and scored 14.05.
Across the whole corpus, the runs that reached a second evaluated node are `accEE`, `remEE`,
`remEE2`, `remPde3` and `remDL2` — the top score of every task they belong to, except that `accPde`
reached 120.76 on one node. Five points and one exception is not a rule either; it is written here so
the next `discrete_log` probe has something specific to refute.

### 74.3 `remPde5`: one node, 83 % spent before it, and the highest `pde_heat1d` score on record

`remPde5` (pde_heat1d, $1.0059, **TEST 167.2103**) finished while the previous section's ink was
drying and refutes two of the soft patterns this document has been circling.

**Node count does not decide it.** §74.2 recorded that the five runs reaching a second evaluated node
held the top score of every task they belong to, with `accPde` as the one exception, and asked the
next probe to refute it. `remPde5` did: ONE evaluated node (161.5536 train, 167.2103 test), and it is
now the highest `pde_heat1d` number in the corpus, above `remPde3`'s 129.75 which had two. Two
exceptions out of six on this task is not a pattern with an exception; it is no pattern.

**Spend before the first node does not decide it either.** `remPde5` spent 83 % of its dollar before
that node existed — inside the band §72 flagged as the waste signature (`remPde` 91 %, `remPde2`
79 %, `remPde4` 85 %) and far above the runs that scored well by the earlier reading. `pde_heat1d`
now reads:

| probe | TEST | nodes | before first node | `eval_train` |
|---|---|---|---|---|
| remPde5 | **167.2103** | 1 | 83 % | 21 |
| remPde3 | 129.75 | 2 | 55 % | 26 |
| accPde | 120.7621 | 1 | 53 % | 18 |
| remPde | 54.1227 | 1 | 91 % | 22 |
| remPde2 | 30.3282 | 1 | 79 % | 21 |
| remPde4 | 0.0 | 1 | 85 % | 11 |

83 % sits between the 79 % that scored 30 and the 85 % that scored 0, and it scored 167. Six points,
0.0 to 167.21, and none of the three cheap summary statistics this document has reached for — node
count, spend-before-first-node, `eval_train` — orders them.

What that leaves is §73's conclusion, now with six points on one task instead of three: the
per-configuration spread is enormous, and single-run comparisons on `pde_heat1d` mean nothing. The
statistics kept failing because they were being asked to explain variance that is not yet shown to
have structure.

## 75. The step ceiling, evaluated against the corpus it was designed from

The per-step money ceiling (half of what remains, never below a fifth of the run) was chosen from
seven observed steps. The corpus now holds **81** `plan_step` sessions across thirteen probes, so the
rule can be checked rather than argued.

**The wall binds rarely, and unevenly.** Six of the 81 steps (7 %) ran to the 1200 s session wall.
What they cost varies more than threefold:

| probe | duration | step cost | budget left when it started | share of what remained | would the ceiling cut it? |
|---|---|---|---|---|---|
| remPde | 1212 s | $0.4820 | $0.7331 | 66 % | **yes** |
| remPde5 | 1236 s | $0.3276 | $0.6993 | 47 % | no |
| accPde | 1212 s | $0.2822 | $0.8941 | 32 % | no |
| remDL3 | 1213 s | $0.1609 | $0.7241 | 22 % | no |
| remDL3 | 1211 s | $0.1587 | $0.5632 | 28 % | no |
| remDL2 | 1202 s | $0.1484 | $0.7370 | 20 % | no |

Same wall, same 20 minutes, and between $0.15 and $0.48 of a $1.00 run — which is the original point
restated with six observations instead of two: seconds are not dollars, and bounding one does not
bound the other.

**The ceiling bites once, and it is the right once.** Of the six wall-hitters it cuts only `remPde`'s
$0.4820 — the runaway it was built for. Across all 11 steps in the corpus costing more than $0.15 it
would cut 3. It notably does NOT cut `remPde5`'s $0.3276 step, and `remPde5` scored **167.2103**, the
highest `pde_heat1d` number on record (§74.3). A tighter rule — half of remaining with no floor, or a
flat quarter — would have cut that step, and on the evidence available that would have been a change
for the worse.

So the rule stays as shipped. This section exists because the alternative was to tighten it on the
intuition that 7 % of steps hitting a wall sounds like a lot; the corpus says the wall is rare, its
cost is what varies, and the one step worth cutting is already the one being cut.

**Verified 2026-09-01, one sweep later.** Four probes carrying the trace have now closed plans, and
their `plan_steps` spans carry `cut_steps: []` with `cutoff: None` on every step. So the field lands
where it was wired to land, and the answer it gives is that nothing has been cut yet — a measurement
rather than an absence of evidence, which is the whole difference this trace exists to make. The
table above remains a retrospective; the ceiling has still never fired, and now that can be said.

## 76. Time to the first build step: useful for triage, useless as a predictor, and I nearly said otherwise

Two live `discrete_log` probes sat at 55 minutes with $0.14 and $0.18 spent, zero `eval_train` calls
and zero `plan`/`plan_step` generations, while an `edge_expansion` probe started at the same minute
had spent $0.52, made 12 `eval_train` calls and evaluated a node. That looked like two stuck runs.

They were on schedule. Minutes from a run's first span to its first `plan_step` generation:

| task | runs | time to first build step |
|---|---|---|
| edge_expansion | 5 | 18, 20, 21, 21, 30 |
| pde_heat1d | 6 | 12, 19, 19, 29, 50, 57 |
| discrete_log | 2 | 64, 74 |

`discrete_log`'s two completed runs are the two slowest of the thirteen, and the two live ones at 55
minutes with no build are consistent with that rather than with a fault. That is the whole of what
this measurement is for: **a sweep that cannot tell a slow task from a stuck run spends an
investigation on every one of them**, and this one cost four commands before the answer arrived.
`probe_summary.py` now carries the column.

### 76.1 It is not a predictor, and the way I found that out is the point

The first version of this section said the three tasks separate cleanly — edge_expansion 18-21,
pde_heat1d 29-33, discrete_log 64-74 — computed over the eight runs I happened to list. The full
fifteen do not separate: `pde_heat1d` runs from 12 to 57 minutes and overlaps `edge_expansion`
entirely. The clean bands were an artefact of which runs I typed into the script.

Then, computing the correlation with TEST, I got r = −0.81 on `pde_heat1d` (n = 5) — a strong signal,
and the first thing in four sweeps that looked like structure. It was wrong the same way: my data
literal held five of the task's six runs. The missing one is `remPde4`, which reached its first build
step at **19 minutes** — the same minute as `remPde3` — and scored **0.0** against `remPde3`'s
**129.75**. With all six the coefficient is −0.40; on `edge_expansion` it is −0.01.

So this is the fourth summary statistic to fail, after node count, spend-before-first-node and
`eval_train` (§74.3), and it failed by the same mechanism twice in one hour: a subset that separated
cleanly, and a second subset that dropped the single run which destroys the relation. Both times the
subset was mine, chosen by hand, and neither was chosen to prove anything — which is exactly why it
is worth writing down. The corpus keeps offering clean-looking structure to anyone willing to list
the runs themselves.

What survives is narrow and operational: build time is a property worth SEEING, so a slow task is not
mistaken for a broken run. It orders nothing.

## 77. A third of plan steps write nothing, and it costs 4 %

`plan_step` is 41.7 % of everything this corpus has spent ($5.31 of $12.71), so its waste is worth
counting rather than estimating. Across the 14 runs that closed a plan — 55 steps in all — **20 steps
(36 %) wrote no file at all**, and the second step of the plan is one of them in **10 of the 14
runs**.

That sounds like a third of the budget. It is not:

| | $ |
|---|---|
| all plan steps | 5.7624 |
| steps that wrote nothing | **0.4950** |
| share of step spend | 9 % |
| share of everything the corpus has spent | **4 %** |

A step that writes nothing is a step that ended early, so it is cheap by construction: 36 % of the
count is 9 % of the money. Two runs are exceptions — `remPde3` lost 15 % of its dollar to empty steps
and `remEE` 12 % — and the median run loses 1-3 %.

So the honest conclusion is that this is not the lever. §72 measured the same phenomenon from the
other side (26 % of steps were a measurement and nothing else) and the card gained a clause telling
the planner not to spend a step on a measurement the engine runs for free. Whether that clause
lowered the count cannot be read off these numbers: 26 % and 36 % are different questions —
"planned only a measurement" and "changed no file" — and comparing them would be the same mistake as
§76.1's hand-listed subsets, one denominator further out.

What is worth keeping is the shape: the second step is usually empty, the third usually rewrites what
an earlier one wrote (`superseding_steps` names step 3 in **11 of 14** runs). A plan of three to five steps
is, in practice, one or two steps that write and a tail that mostly does not. That is a fact about
how the plan is used, at a cost of 4 %, and it is recorded rather than acted on.

## 78. §69.1's acceptance test cannot be run here, and the tool was reporting the wrong units

§69.1 pinned a comparison before its data arrived: the reference-module clause is accepted or
rejected against **4.9–8.3 %**, the share of `run_probe` calls that import the reference, measured on
three probes carrying the OLD card. Two things were wrong with how that has been carried since.

**The units.** `probe_summary.py` reported raw regex hits — "115 imports / 1350 is_solution+generate
calls" — against a percentage baseline. A count cannot be compared with a share, and this sat in the
tool for three sweeps while the brief carried the band on every page. It now reports the rate over
`run_probe` calls, with the baseline printed beside it, and counts CALLS rather than occurrences: one
call importing the reference five times is one call.

**The control group is gone.** All sixteen probes on this box carry the clause — it landed
2026-08-30, and the earliest surviving probe ran 2026-08-31. The three runs the 4.9–8.3 % band was
measured on (dsCH6, dsRBF2, dsPde2) were in `/var/tmp` when it was wiped on 2026-08-29. So there is
no before-group here and the acceptance test as pinned cannot be run, however many probes accumulate.

What the sixteen do say, for whatever a one-armed measurement is worth:

| | n | median | range |
|---|---|---|---|
| all probes on this box (all carry the clause) | 16 | 7.3 % | 0.0 – 18.8 % |
| §69.1's pre-clause band | 3 | — | 4.9 – 8.3 % |

The median lands inside the old band and the range is four times wider than it. That is compatible
with no effect and with a large one, and it is not evidence for either.

Recorded because §69.1's whole point was that the goalposts must not move: the honest outcome is not
a verdict but "the experiment lost its control arm to an unrelated crash, and no amount of new data
restores it". A clean answer needs a deliberate arm with the clause removed — a between-arms change
under §73.3's rule, several runs each — and that is a decision about spending, not a sweep task.

## 79. What the champion rule has now saved: 2 %, 59 %, 95 %

`remEE4` finished at **TEST 262.0356**, the highest `edge_expansion` score in this corpus, and its
three evaluated nodes ran **178.9456 → 265.7918 → 12.9883**. The run submitted the second.

That is the third time the "best EVALUATED node is kept, not the last" rule has bitten, and the
three cases together are the first measurement of what the clause is worth:

| probe | nodes (train) | submitted | last | cost of submitting the last |
|---|---|---|---|---|
| remDL2 | 14.295, 13.982 | 14.295 | 13.982 | 2 % |
| remPde3 | 123.1297, 50.0147 | 123.1297 | 50.0147 | 59 % |
| remEE4 | 178.9456, **265.7918**, 12.9883 | 265.7918 | 12.9883 | **95 %** |
| remDL5 | **11.5564**, 1.9865 | 11.5564 | 1.9865 | **83 %** |
| remEE6 | 22.5791, **234.8928**, 148.7194, 0.0 | 234.8928 | 0.0 | **100 %** |

§72 recorded `remDL2` as the first empirical demonstration and called it thin — a 2 % difference on
one run. Three runs later the same rule was standing between a corpus-best 262 and a 13, and
`remDL5` and `remEE6` (both 2026-09-01) make five: 2 %, 59 %, 95 %, 83 %, **100 %**. Four of the five
cost more than half the run's score, so the 2 % that made §72 call it thin is the outlier, not the
type. `remEE6` is the limit case — its fourth and last node scored **0.0** (its Cython kernel failed
to compile), so submitting the last node would have submitted nothing that runs, on a run that
scored 232.7736. Nine of the
seventeen probes here reached exactly one node and one reached none, so ten could not exercise the
rule at all; of the seven that reached two or more, three ended on a node worse than their best.

The clause telling the model this (`KEEP_BEST`, shipped 2026-08-31) is aimed at the other side of it:
a model that does not know the rule has every reason to protect a working solver rather than attack
it, and `remEE4`'s third node is what attacking it looks like when it fails. The engine already kept
the good one; what the card adds is the model knowing it will.

**Not shown:** that the clause caused any of this. `remDL2` predates it. `remEE4` and `remPde3` carry
it, and both also carry every other change of that batch. What the table measures is the RULE's
value, which is a property of the engine, not the card's effect on behaviour — and separating those
needs the arm §78 says this corpus cannot supply.

The money ceiling still has not fired: `remEE4`, `remPde6` and `remDL5` all closed plans with
`cut_steps: []` — the third, fourth and fifth runs to report it (§75).

## 80. Three probes ran on the other instrument, and one of them is §73's controlled pair

The meter records `stream` per call. Across 4,260 metered calls over 1,000 tokens:

| probe | unstreamed | streamed | 504 at the ceiling | wall clock lost |
|---|---|---|---|---|
| remEE | 309 | 0 | **9** (2.8 %) | **45 min** |
| accEE | 291 | 0 | 0 | — |
| accPde | 240 | 0 | 1 (0.4 %) | 5 min |
| remDL (abandoned) | 27 | 0 | 11 | 55 min |
| every other probe | 0–5 | 95–327 | **0** | — |

`accEE`, `accPde` and `remEE` ran before the bench profile was made to SET `LOOPLAB_LLM_STREAM`
rather than default it (2026-08-31, §fix `${VAR:-1}` loses arguments). Without streaming the
gateway's nginx measures the whole generation against a 300 s window, and `remEE` lost nine calls to
it — five minutes each, forty-five minutes of wall clock that returned nothing, on a run whose budget
is a dollar.

**This breaks §73's controlled pair.** That section set `remEE` (179.6451) beside `remEE2` (102.175)
— byte-identical 15,553-character card, same task, same $1.00, four hours apart — and concluded a
floor of about 1.8× under the noise of a single configuration. The card was identical; the
instrument was not. `remEE` ran unstreamed and lost 2.8 % of its calls; `remEE2` ran streamed and
lost none. Whatever that pair measures, it is not one configuration observed twice, and the 1.8×
floor does not follow from it.

What survives §73 is the part that never depended on that pair: `accEE` 224.8846, `remEE3` 193.6729,
`remEE4` 262.0356 and `remEE2` 102.175 span 2.6× on `edge_expansion`, and §50 had already measured
19× across 27 runs of that task. The floor was never the load-bearing claim; it was the tidy one.

**And it is one more reason §78's control arm cannot be reconstructed.** Three of the seventeen
probes here are on a different instrument from the other fourteen, which is not visible in any
score, any card, or any run log — only in a `stream` field on the meter rows. Nothing in the probe's
own evidence records which instrument it ran on, and that is worth fixing before the next comparison
is drawn: `ENVIRONMENT.txt` in each snapshot records the setting, but a probe tree does not.

## 81. The card batch, tested at last: nothing shown, and the best number was the instrument

The corpus has reached twenty scored probes, which is the first point at which §73.3's rule — several
runs of each configuration before a comparison says anything — can actually be met. So: did the batch
of card changes shipped 2026-08-31 (the 10× per-instance ceiling, KEEP_BEST, and the clauses beside
them) raise the score?

Exact one-sided permutation rank-sum, new card against old, per task:

| task | old card | new card | p (all runs) | p (streamed only) |
|---|---|---|---|---|
| edge_expansion | 102.2, 179.6, 224.9 | 193.7, 227.4, 232.8, 262.0 | 0.057 | **0.200** |
| pde_heat1d | 30.3, 54.1, 120.8 | 0.0, 125.9, 129.8, 133.5, 167.2 | 0.125 | 0.190 |
| discrete_log | 14.0 | 7.1, 7.6, 12.2 | 1.000 | 1.000 |

**Nothing is shown.** Two tasks lean the right way, one leans the other, none reaches any threshold
worth naming, and `discrete_log`'s single old-card point is not a comparison at all.

**And the most promising number is an artefact of the instrument.** `edge_expansion`'s p = 0.057 rests
on `accEE` (224.9) and `remEE` (179.6) being in the OLD group — and both ran unstreamed (§80),
`remEE` losing nine calls to the gateway's 300 s ceiling. Restrict to the single instrument every
current probe uses and the old group is one run, p = 0.200. The apparent effect was two-thirds
composed of runs that are not comparable with either group.

That is the same shape as the six summary statistics before it (§74.3, §76.1, and the build-duration
gap that died in thirty minutes): a number that looks like structure until the thing it is actually
tracking is named.

**What would settle it.** Not more probes on the new card — that arm has 12 and adding to it moves
nothing. The old card has 4 runs on the current instrument, one of them per task on two of the three
tasks. A deliberate old-card arm, several runs per task, is the only thing that answers this, and it
costs a dollar a run to buy back a comparison the batch was shipped without.

**Not tested, and worth keeping separate:** §79's champion-rule tally (2 %, 59 %, 95 %, 83 %, 100 %)
is a property of the ENGINE and does not depend on any of this. The rule fires whether or not the
card mentions it; what the card changes is whether the model knows it will, and that is exactly what
this section cannot yet measure.

## 82. The step-cutoff trace fired, and it says the money ceiling still has not

`remDL6` (discrete_log, $1.0103, TEST **4.0326**) is the first run in the corpus whose `plan_steps`
span carries a non-empty `cut_steps`. §75 recorded the ceiling as never having fired and §79 repeated
it for five runs; this is what the trace was added to be able to say, and what it says is narrower
than "it fired".

    план: total=4  noop=[2]  cut=[1, 4]
      step 1  cutoff='time'  wrote solver.py                                  $0.1425  1300 s
      step 2  cutoff=None    wrote nothing (noop)                             $0.0167    19 s
      step 3  cutoff=None    wrote solver.py                                  $0.0718   503 s
      step 4  cutoff='time'  wrote rho_dlog.pyx, setup.py, solver.py          $0.2250  1264 s

**Both cuts are `time`** — the 1200 s session wall, at 1300 s and 1264 s. The money ceiling did not
fire, and this run is the closest it has come:

| step | spent | budget left at its start | ceiling | fired? |
|---|---|---|---|---|
| 1 | 0.1425 | 0.8244 | 0.4122 | no |
| 2 | 0.0167 | 0.6819 | 0.3410 | no |
| 3 | 0.0718 | 0.6653 | 0.3326 | no |
| 4 | **0.2250** | 0.5935 | **0.2967** | no — 76 % of the way |

Three things this settles, none of them the one being watched for:

**The trace works.** It names the bound, the step, its title and what it wrote, and it did so on the
first run that had anything to name. Before it, a cut step was indistinguishable from a finished one
in every run tree on this box (§75).

**A cut step is not a lost step.** Both cut steps WROTE — step 4 produced `rho_dlog.pyx`, `setup.py`
and `solver.py` after being cut. The salvage this was designed around is doing its job, which is why
cutting is a cheaper intervention than it sounds.

**The two bounds do not fire together.** §75 argued from 81 steps that seconds are not dollars; here
is the same argument from one step, on the other side. Step 4 hit the wall at 1264 s having spent
76 % of what it was allowed to spend — so on this run the wall was the binding constraint and the
money ceiling was slack, exactly as the retrospective predicted for the five wall-hitters it would
not have cut.

`remDL6`'s 4.0326 is the lowest `discrete_log` score on record, and this section does not claim the
cuts caused it — one run, and the two cut steps are the two that wrote the most.

## 83. A plan for deciding the open questions, sized from the spread we actually have

Every open question about the loop is an A/B, and until now none has been sized. Here is what the
corpus says it costs to answer one, and which questions are therefore worth asking.

### 83.1 What the noise permits

Within-task spread over the runs on the current instrument:

| task | n | range | ratio | CV |
|---|---|---|---|---|
| edge_expansion | 8 | 102.2 – 276.7 | 2.7× | **0.27** |
| discrete_log | 6 | 4.0 – 16.8 | 4.2× | 0.43 |
| pde_heat1d | 9 | 30.3 – 167.2 | 5.5× | 0.54 |

`edge_expansion` is the quietest by a factor of two, so **experiments belong there** and nowhere
else. Running an A/B on `pde_heat1d` at CV 0.54 is buying noise.

Exact one-sided rank-sum, best case, is `1/C(2n,n)`: **n = 3 per arm can never beat p = 0.05**, and
only then if the arms separate perfectly — which a 2.7× within-arm spread makes unlikely. Power,
resampled from the observed `edge_expansion` distribution, 300 trials per cell:

| true effect | n = 4 | n = 6 | n = 8 |
|---|---|---|---|
| 1.25× | 25 % | 50 % | 61 % |
| 1.50× | 41 % | 67 % | 78 % |
| 2.00× | 63 % | 92 % | 95 % |

So the floor for any loop experiment is **6 runs per arm on `edge_expansion`**, and that buys a
two-in-three chance of seeing a 1.5× effect. Anything smaller than 1.25× is not detectable at any
sample size this stand will pay for. Every "n = 3 looked clean" in §74-§76 is explained by this table.

### 83.2 The queue, cheapest-decidable first

**1. The card batch (§81). $6, decidable.** The new-card arm already has 5 `edge_expansion` runs
(remEE3-6, remEE8, plus remEE9 running). The old-card arm has ONE on this instrument (remEE2 —
accEE and remEE ran unstreamed, §80). Six deliberate old-card runs on `edge_expansion` completes it.
This is the only open question the stand can settle at this price, and it is the one that decides
whether the 2026-08-31 batch was worth shipping.

**2. The reference-module clause (§78). $6, decidable, and currently unanswerable.** Same shape: an
arm with the clause REMOVED, six runs on `edge_expansion`. Its control was destroyed with /var/tmp
on 2026-08-29 and no quantity of new probes rebuilds it.

**3. Arm A versus arm B — the programme's actual question. p = 0.75 over 10 comparable tasks
(docs/58). NOT an `edge_expansion` experiment** and not sized by the table above: it pools tasks
rather than repeating one, so its power comes from task count, not run count. What it needs is arm A
re-measured on the verified ruler for the 10 tasks that have no arm-A number. Cost is arm A's own
budget, not $1/run, and it should be quoted before it is started.

**4. The variance mechanism. Not an A/B at all.** Six summary statistics have failed (§74.3, §76.1,
and the build-duration gap that died in thirty minutes), so a seventh is not the move. The corpus now
holds a 102.2 and a 276.7 from the SAME card on the SAME task; the question is what differs between
those two artefacts, which is read by diffing two champions and their traces, not by correlating a
column. Free, and unscheduled because it needs attention rather than money.

**5. The money ceiling (§75, §82).** It has never fired; the 1200 s wall fires instead, and its
closest approach was 76 % of the cap. Nothing to decide until either bound moves, and moving one to
see what happens is a change to the instrument mid-corpus. Left alone deliberately.

### 83.3 What this plan refuses

It does not queue more probes on the CURRENT configuration. That arm has 8 `edge_expansion` runs and
a ninth changes nothing anyone is asking. Idle-lane probes have been filling it by default; from here
a free lane should take an arm from the queue above or stay idle, because an unpaired run costs a
dollar and answers nothing.

## 84. The champion rule is worth 11 runs out of 17, and the corpus already knew

§72.2 demonstrated that the best EVALUATED node is submitted rather than the last, and cited
`remDL2` -- node_0 at 14.29 against node_1 at 13.98 -- as the first direct evidence that the rule
bites rather than merely holds. The comment above `KEEP_BEST` in `make_task.py` still says so. On a
2 % gap that reads like a technicality. It is not one, and no new probe was needed to show it: every
run has recorded every node all along.

Over the 17 probes with more than one evaluated node:

| probe | nodes | best | last | best/last |
|---|---|---|---|---|
| remEE6 | 4 | 234.8928 | **0.0000** | ∞ |
| remPde7 | 2 | 130.8104 | 0.0749 | 1746× |
| remPde9 | 3 | 116.7281 | 3.3451 | 34.9× |
| remEE4 | 3 | 265.7918 | 12.9883 | 20.5× |
| remEE3 | 3 | 194.6503 | 20.5173 | 9.5× |
| remEE7 | 3 | 144.3164 | 18.3794 | 7.9× |
| remDL5 | 2 | 11.5564 | 1.9865 | 5.8× |
| remPde3 | 2 | 123.1297 | 50.0147 | 2.5× |
| remPde8 | 2 | 114.2576 | 70.3424 | 1.6× |
| remDL4 | 2 | 7.0413 | 5.5644 | 1.3× |
| remDL2 | 2 | 14.2947 | 13.9819 | 1.02× |
| *(six more)* | | | | 1.00× (last **was** best) |

**Eleven of seventeen runs ended on a node that was not their best, and not one ended on a node
better than its best-so-far by definition — so the paired sign test over the 11 non-ties gives
p = 1/2048 = 0.00049.** Median submitted TRAIN score is 130.81 under the rule and 18.38 without it,
a factor of 7.1; per task, 2.2× on `edge_expansion` (n=9), 4.5× on `pde_heat1d` (n=4), 1.3× on
`discrete_log` (n=4). `remEE6`'s final act was to score 0.0 — the rule is the only reason that run
has a number at all.

**That table was stale within half an hour, and this is the part worth keeping.** `remPde10`
finished at 12:19 the same day: nodes `[37.4701, 0.0]`, a SECOND run whose last act was to score
zero. Twelve of eighteen, p = 1/4096. Six statistics in this document have died on their next run
(§74.3, §76.1, the build-duration gap); this one survived and got stronger, which is luck, not
method. The method is that the figure is no longer typed here at all —

    python benchmarks/probe_summary.py

prints the whole ledger, the tie count, and the p from the corpus as it stands. Ties are printed
because the sign test's denominator is the non-ties, and a reader who cannot see the ties cannot
check the p; a mutation that counts ties among them reddens `test_the_champion_ledger_counts_ties_
out_of_the_sign_test`. The numbers in the table above are what the tool printed on 2026-09-01 and
are not maintained by hand.

So the loop's last move is worse than its best move about two times in three, and often by an order
of magnitude. `remDL2`'s 2 % was not a typical case, it was the mildest case in the corpus.

**What this does NOT show, and the distinction is the whole of §83's queue.** This measures the
rule's PROTECTIVE value: given the nodes these runs produced, keeping the best is worth 7×. It says
nothing about whether TELLING the model the rule exists changes which nodes it produces — that is a
different claim, it needs the control arm, and `remEEctl1` (launched today on lane 11-21+59-69,
card `--no-unteachable-rules`) is the first run of it. A reader who collapses the two would conclude
from p = 0.00049 that the card clause is proven, when the clause has one run of evidence and it
started this afternoon.

The honest reading of the two together: the rule is demonstrably load-bearing, which raises the
prior that stating it matters, and lowers nothing about the cost of finding out.

## 85. Twelve cut sessions, all by the clock, and no way to tell how close the money came

`_step_cost_ceiling` installs a money bound on every plan-step session: at least
`max(0.5 * (limit - spent), 0.2 * limit)`, so at least $0.20 of a $1.00 probe. §75 and §82 record
that it has never fired and that the 1200 s wall fires instead. That is now measured rather than
believed — and so is the reason the claim could not be checked.

Across all 30 probes, `"cutoff"` appears **12 times, every one of them `"time"`**, in six runs
(`remPde10` four, `remDL4` two, `remDL6` two, `remDL8` two, `remEE7` one, `remPde5` one). Not one
`"cost"`. Every finished run additionally ends on the RUN-level `budget_exhausted`, which is a
different bound from the per-session one and fires 20 times out of 20.

The obvious next question is how close the money ceiling came, and the corpus cannot answer it. The
recorded row is:

    {"step": 4, "cutoff": "time"}

No seconds, no spend, and no session window from which the spend could be reconstructed — I tried,
and every candidate reconstruction matched generation spans rather than tool-loop sessions, which is
how `remDL4` appeared to have a 1820 s "step" that spent $0.07 (it was one long generation, not a
step). So "the wall wins the race" was compatible with two opposite repairs — a ceiling set so high
it is decorative, or one about to bite that merely lost — and nothing in eleven days of probes could
distinguish them.

**Fixed at the source.** `tool_loop.py`'s wall branch now reports the same `"$X of $Y for this
session"` pair the money branch always carried; the money branch is the one that never runs, so the
sentence lived only where it could not be read. `_spend_detail` is shared by both, returns `""`
when there is no accountant (never `$0.0000`, which is a real reading), and says "no money ceiling
set" for the sessions that have none — which is every session except the plan step, and those hit
the wall too. `LLMRepoDeveloper` keeps `seconds` and the spend on a second attribute rather than
widening `last_budget_exhausted`, which other code compares against a KIND
(the `budget-exhausted-vocabulary` claim), and the plan-step row carries both.

**The test hole, found by mutation and worth more than the fix.** The first version of
`test_the_wall_says_what_it_spent.py` exercised `_spend_detail` and `_note_budget` directly and
stayed GREEN under the exact defect: replacing the wall branch's `detail=` argument with `""` broke
nothing, because nothing asserted the wall branch calls the helper at all. Unit tests on both ends
of a wire do not test the wire. The file now drives `drive_tool_loop` with a 0.05 s budget and an
accountant that ticks, and the mutation reddens two tests.

The number this buys is not available yet — it arrives with the next cut session — but from then on
"the money ceiling never fires" stops being a sentence and becomes a column.

## 86. The variance has a mechanism for the bottom of the range, and the first rule derived from it was wrong

§83 item 4 said the variance question is not an A/B and needs two champions diffed rather than a
seventh statistic. Done here, on the extremes of the nine `edge_expansion` runs that share a card:
`remEE8` at 276.7268 against `remEE2` at 102.1750, a 2.71× gap.

**Both runs found the same algorithm.** One C pass over the adjacency list-of-lists, counting edges
whose endpoints differ in membership. No difference in idea at all. Two implementation differences:

1. `remEE8` compiles with `boundscheck=False, wraparound=False, cdivision=True,
   initializedcheck=False`; `remEE2` carries only `language_level=3`.
2. `remEE8`'s kernel takes `nodes_S` — the INDEX LIST — and builds the membership mask itself in a
   `malloc`'d `unsigned char` buffer, doing the division in C too. `remEE2`'s kernel takes a
   ready-made buffer, so its Python `solve()` builds `np.zeros(n)`, fancy-indexes it from
   `np.asarray(nodes_S)` and copies it with `.tobytes()` on EVERY call.

Measured, not reasoned. `remEE2`'s own kernel source recompiled with only the directive line changed:
**0.378 ms → 0.281 ms, a factor of 1.34**. Then both complete per-call paths on a real instance from
`EdgeExpansionTask().generate_problem(n=400, random_seed=1)` (400 nodes, mean degree 8.0, both paths
returning 13.333333):

| path | per call |
|---|---|
| `remEE2` as scored | 17.0 µs |
| `remEE8` as scored | 8.1 µs |
| of which `remEE2`'s per-call numpy setup + `tobytes()` | 3.1 µs (18 % of its own path) |

2.09× against an observed score ratio of 2.71×: same direction, same order.

### 86.1 The first rule died on the corpus, which is the part to keep

The mechanism yields a prediction, so I tested it. Across all ten `edge_expansion` runs, grouped by
whether the champion carries `boundscheck=False` and `wraparound=False`:

| group | n | median TEST |
|---|---|---|
| with both directives | 7 | 193.67 |
| without | 3 | **224.88** |

The wrong way round, p = 0.33 in the predicted direction. `remEE5` (227.35) and `accEE` (224.88)
score near the top with no directives at all; `remEE7` (144.24) scores near the bottom with them.
The micro-benchmark is not wrong — the 1.34× is real — it is simply not what separates these runs.
That is the eighth statistic in this document to die on the corpus, and the first to die after being
derived from a physical measurement rather than a correlation, which is worth knowing about the
method: a mechanism you can time in isolation still has to be shown to be the one that bites.

### 86.2 The second rule holds, and is POST-HOC

Reading the survivors is what suggested it: `remEE5` and `accEE` lack the directives but pass
`nodes_S` straight into C. So the split that matters may be difference 2, not difference 1 — where
the membership mask is BUILT:

| group | n | median TEST | runs |
|---|---|---|---|
| kernel takes the index list, mask built in C | 7 | 227.35 | 169.66 – 276.73 |
| kernel takes a ready-made buffer | 2 | 123.21 | 144.24, 102.18 |

Exact one-sided rank-sum p = 0.0278. **This is the SECOND rule tried on the same ten runs, and it
was suggested by looking at which runs survived the first.** Two tries make the naive corrected p
about 0.056, and "suggested by the survivors" is not a correction any arithmetic fixes. What it has
that the directive rule did not is an independent physical measurement of the same quantity — the
3.1 µs of per-call marshalling, 18 % of the slower path, measured before the corpus was scored on
it. That makes it a hypothesis worth a deliberate arm, not a result.

It also explains only the BOTTOM of the range. The seven indices-in runs still spread 169.66 to
276.73, a factor of 1.63, and nothing here touches that.

### 86.3 What is NOT being shipped, and why

The card tells the model that `__init__` is on the clock. It does not say that data marshalled into
a compiled kernel is on the clock ON EVERY CALL, which is what cost `remEE2` 18 % of its per-call
time before any counting began. That is the obvious candidate clause and it is **not going in
today**: four control probes are running under `card_sha256 d20e9c0e0b3eb26f`, and editing the card
mid-arm would leave four dollars measuring a card that no longer exists. It goes in the queue behind
the arm now in flight — which is the same discipline §83 asked for and the first time it has cost
anything to keep.

## 87. First reading from the control arm, at n = 2, on the quantity the removed clause names

`KEEP_BEST` ends: *"Measure early, measure often, and spend late time on attempts rather than on
caution."* So the quantity it targets is how much a run spends before its FIRST evaluated node, and
that number is fixed the moment the first node lands — it does not drift as the run continues, which
makes it readable from a probe still in flight. This is pre-registered in the sense that matters:
the clause names it, and it is the first thing measured after the arm produced anything at all.

`edge_expansion`, dollars spent before the first evaluated node:

| arm | n | median | range |
|---|---|---|---|
| shipped card | 10 | $0.3119 | $0.2440 – $0.4771 |
| `--no-unteachable-rules` | 2 | $0.4575 | $0.3730 – $0.5421 |

Exact one-sided rank-sum p = 0.0303. **The floor at n = 2 against n = 10 is 0.0152**, so this is one
step off the best a two-run arm can produce and there is no room below it worth chasing: `remEEctl1`
at $0.5421 is outside the whole shipped range, `remEEctl2` at $0.3730 is not — it sits seventh of
twelve.

**Two reasons this is a reading and not a result.**

n = 2 against §83's table, which puts a two-run arm below every useful power figure. Four runs are in
flight; six is the floor for 67 % power against a 1.5× effect, and this metric is not the score
anyway.

And the statistic is CENSORED in the direction that flatters the control. It can only be computed
for a run that reached a first node. If the clause does what it says, the arm without it should
produce runs that evaluate late *or never* — and "never" leaves this table rather than landing at
the far end of it. `remEEctl3` and `remEEctl4` have no first node yet; whichever way they resolve,
the censoring has to be handled before this number means what it appears to mean. The fix is to
report time-to-first-node with the non-evaluators kept in at their run's total, not to add runs.

### 87.1 The same quantity in minutes, at n = 4, and stronger

All four control probes have now reached a build step, so the other unit of "measure early" is
readable and — unlike the dollars — barely censored: a run that has started building has a number
before it evaluates anything.

Time from run start to the first `plan_step`, `edge_expansion`:

| arm | n | median | sorted |
|---|---|---|---|
| shipped card | 10 | 21.5 m | 14, 18, 20, 21, 21, 22, 24, 29, 30, 49 |
| `--no-unteachable-rules` | 4 | 50.0 m | 38, 46, 54, 66 |

Exact one-sided rank-sum p = **0.0040**, floor 0.0010 at n = 4 against n = 10. Not clean separation:
`remEE9` took 49 m on the shipped card, inside the control range.

**This is not a second confirmation of §87.** One construct, two units, measured on overlapping runs
— reading p = 0.0303 and p = 0.0040 as independent evidence would be wrong arithmetic about the same
observation. It is the better-measured of the two, because it covers 4 of 4 control runs rather than
2 of 4, and both are printed on the same row of `probe_summary.py` for exactly that reason.

**And it is a PROCESS metric, not the outcome.** §83 sized n = 6 for the SCORE. A clause can move
when a run starts building and leave the score untouched; the control arm has produced no TEST score
at all yet. What can be said today is narrow and worth saying plainly: removing those two clauses
delays the first build, from a median of 21.5 minutes to 50. Whether that costs anything is the
question the arm is still running to answer.

### 87.2 The censoring resolved, and it did not bite

§87 reported the dollars figure at n = 2 and named the reason it might be worthless: the statistic
can only be computed for a run that reached a first node, so an arm that evaluates LATE OR NEVER
loses its worst members to the exclusion rather than to the far end of the range. `remEEctl3` and
`remEEctl4` were the two excluded runs.

Both have now evaluated, and the reading got stronger rather than weaker:

| arm | n | median | sorted |
|---|---|---|---|
| shipped card | 10 | $0.3119 | .2440 .2442 .2621 .2764 .3055 .3184 .3273 .3291 .3655 .4771 |
| `--no-unteachable-rules` | 4 | $0.4127 | .3659 .3730 .4524 .5421 |

Exact one-sided p = **0.00699**, from 0.0303 at n = 2. The two arrivals landed at $0.3659 and
$0.4524 — one below the shipped maximum, both above the shipped median. The ranges overlap; the
separation is not clean and never was.

The caveat was worth writing and it did not come true. That is the outcome to record precisely,
because the opposite outcome is the one that would have been quietly skipped: had those two runs
come in low and killed the finding, a reader of §87 alone would have had no way to know the number
had been provisional. Both metrics now stand at n = 4 on the same construct — $0.4127 against
$0.3119 (p = 0.00699) and 50.0 m against 21.5 m (p = 0.0040) — and neither is a score. The control
arm has still produced no TEST number at all.

## 88. The control arm's first score: 34.76 against a shipped range of 102 to 277

`remEEctl1` finished. It is the first `--no-unteachable-rules` run to produce a TEST number, and the
number is far outside everything the shipped card has produced on `edge_expansion`.

| | remEEctl1 (control) | shipped card, n = 10 |
|---|---|---|
| TEST | **34.7566** | 102.18 – 276.73 |
| nodes (train) | 35.0197, 34.8131 | |
| champion | **49 lines of plain Python** | Cython kernel in all ten |
| eval_train calls | 35 | 19 – 32 |
| spend before first node | 54 % | 24 – 47 % |
| spend after last node | 0 % | |
| reference use | 6.9 % import / 6.9 % is_solution | §69.1 baseline 4.9 – 8.3 % |

The champion is the finding. Every one of the ten shipped-card runs shipped a compiled kernel
(37–70 lines of Cython); this run shipped plain Python and scored 2.9× below the lowest of them.
It also ran the MOST `eval_train` calls of any `edge_expansion` probe — 35 against a shipped range
of 19–32 — so it was not idle, and it was not starved of measurements. It measured more and built
less.

**n = 1.** §83's table says a one-run arm settles nothing, and this changes none of that. What it
does is make the next four runs worth watching for one specific thing — whether the control arm
compiles at all — which is a sharper question than "is the score lower", and answerable at a smaller
n because it is nearly binary. Four control runs are in flight (`remEEctl2`–`remEEctl5`).

### 88.1 Its score was destroyed and recovered, and the recovery nearly lied

`final.json` was ZERO BYTES when I looked. The score had been written and printed — the probe log
holds `{"speedup": 35.0981, ...}` — and then the `run_probe.sh` offset hazard (§dcdf1f29) resumed
the shell at a stale offset, re-parsed the scoring block, and applied its `> "$OUT/final.json"`
redirect to nothing. One file across all 29 probes; every other `final.json` is intact.

Recovered by re-scoring the preserved champion rather than by rerunning the probe. And the first
attempt at that returned **1.0002**, which I came close to filing as the real number. It was run
without `ALGOTUNE_BASELINE_CACHE_DIR` — one missing environment variable, a factor of 35, because
the cache IS the denominator of every speedup. Repeating it under the environment the probe's own
INSTRUMENT.txt records gives 35.0205, and a third scoring under load gives 34.7566: three readings
0.7 % apart. The recorded row carries a `recovered_note` saying which it is.

The instrument had reported this probe as "STILL RUNNING (no stated reason)" — a finished,
fully-paid run filed under "not done yet". That is fixed too: a zero-byte `final.json` now reports
as a destroyed score, and says whether the champion survives to re-score.

## 89. The second control run compiles, scores 184, and refutes yesterday's sharp question

`remEEctl2` finished. §88 proposed watching one nearly-binary thing — whether the control arm
compiles at all — because `remEEctl1` had shipped 49 lines of plain Python where all ten
shipped-card runs shipped Cython. Two runs in, the answer is no:

| run | TEST | champion | eval_train | before % |
|---|---|---|---|---|
| remEEctl1 | 34.7566 | 49 L plain Python | 35 | 54 % |
| remEEctl2 | **184.2220** | **70 L kernel** (`edge_scan.pyx` + `setup.py`) | 31 | 37 % |
| shipped card, n = 10 | 102.18 – 276.73 | Cython in all ten | 19 – 32 | 24 – 47 % |

`remEEctl2` compiled, landed inside the shipped range, and spent 37 % before its first node — inside
the shipped range for that too. The control arm's two scores are 34.76 and 184.22, a factor of 5.3
between them, which is twice the whole shipped-card spread on this task (2.7×).

So the picture after two runs is: the process metrics still separate (§87, §87.1 — dollars and
minutes to the first build, both at n = 4 including the two unfinished runs), and the SCORE does
not separate at all. Those are compatible: a clause can change when a run starts building without
changing what it eventually ships. It is also exactly what §83 warned the corpus would look like at
small n on a task whose within-arm spread is 2.7×, and why it put the floor at n = 6.

What §88 got wrong is worth stating plainly rather than quietly dropping: it read one run's plain
Python champion as a candidate pattern, and the very next run refuted it. The section said "n = 1"
and "changes none of that", which was the right hedge — and the hedge is the only reason this is a
correction of a hypothesis rather than of a claim.

### 89.1 The same file was destroyed again, and the instrument caught it this time

`remEEctl2`'s `final.json` was also zero bytes. It was launched at 12:35, before the offset hazard
was fixed at 15:35, so it ran the poisoned bytes; `remEEctl3` and `remEEctl4` were too and will lose
theirs the same way. `remEEctl5` launched after the fix.

The difference from §88.1 is that nothing had to be noticed. Yesterday the summary said "STILL
RUNNING (no stated reason)" and it took a directory listing to find the zero-byte file. Today it
said, on the first run of the tool after the event:

    remEEctl2  -- STILL RUNNING final.json is ZERO BYTES -- the score was written and then
                  destroyed; re-score it: the champion is preserved

— correct about the destruction and its remedy, and wrong in its first two words, which is its own
defect and fixed in 82f8b4ea. Recovery gives 184.222 against the 186.7953 in the log, 1.4 % apart.

### 89.2 The score at n = 3: p = 0.08, and that is the number §83 asked for

`remEEctl3` finished at 169.3404 — a 41-line Cython kernel, three nodes, and its LAST node scored
0.0, the fourth zero-ending run in the corpus and another save by the champion rule. Its
`final.json` survived, unlike remEEctl1's and remEEctl2's: the offset damage is byte-position
dependent, so "launched before the fix" predicts exposure, not certainty.

The arm's three TEST scores against the shipped card's ten:

| arm | n | median | range | ranks among all 13 |
|---|---|---|---|---|
| shipped card | 10 | 209.28 | 102.17 – 276.73 | |
| `--no-unteachable-rules` | 3 | 169.34 | 34.76 – 184.22 | 1, 4, 7 |

Exact one-sided rank-sum p = **0.0804**, floor 0.0035. Not significant, and §83 said in advance that
n = 3 settles nothing — its power table puts even a 2.0× effect at 63 % detection with n = 4.

**This is the pre-registered outcome.** The dollars (§87, p = 0.0070) and the minutes (§87.1,
p = 0.0040) are process metrics; the score is what the programme is about and what §83 sized n = 6
for. As of this sweep the two disagree in exactly the way the plan anticipated they might: removing
the clauses reliably delays the first build, and has not been shown to change what gets shipped.

`remEEctl4`, `remEEctl5` and `remEEctl6` are in flight, which takes the arm to six.

## 90. The process finding is dissolving as n grows, which is what §83 said it would do

§87 and §87.1 reported that removing the two clauses delays the first build, on two units of one
construct, and §87.2 checked the censoring caveat and called it resolved. Four more control runs
later, both readings have moved back toward nothing:

| metric | n = 2 | n = 4 | now |
|---|---|---|---|
| dollars before first node | p = 0.0303 | p = 0.0070 | **p = 0.0276** (n = 5) |
| minutes to first build | — | p = 0.0040 | **p = 0.0492** (n = 6) |

`remEEctl5` reached its first build in 24 minutes and `remEEctl6` in 17 — the shipped card's range
is 14–49, so both landed inside it, at the fast end. The control arm's build times now read
17, 24, 38, 46, 54, 66 against the shipped 14, 18, 20, 21, 21, 22, 24, 29, 30, 49. Median 42.0
against 21.5, and an exact one-sided p that has walked from 0.004 to 0.049 by adding two runs.

The score never separated at all (§89.2, p = 0.0804 at n = 3).

**This is the ninth statistic in this document to move against itself, and the first where I had
already written a caveat, watched it resolve favourably, and said so.** §87.2's sentence — "the
caveat was worth writing and it did not come true" — was accurate about the censoring and wrong as
reassurance: the reading survived the two runs that had been excluded, and then weakened on the two
that came after. Surviving one specific objection is not the same as being stable, and a section
that says "the caveat did not bite" invites exactly that conflation.

What holds up, and it is not nothing: the arm's build times still have a much wider spread than the
shipped card's (17–66 against 14–49) and a higher median. What does not hold up is any claim that
the difference is established. §83 put the floor at n = 6 for the SCORE against a 1.5× effect, the
score is at n = 3, and this is what a small-or-absent effect looks like on the way there.

No number in this section was typed by hand. `probe_summary.py` prints the by-card rows on every
run, which is the only reason the reversal was seen the same afternoon rather than quoted stale for
a week — the discipline §84 bought after being wrong for thirty minutes.

## 91. The control arm reached §83's floor and the answer is "not shown"

`remEEctl5` and `remEEctl6` finished, taking the `--no-unteachable-rules` arm to six scored runs —
the sample size §83 set as the floor, chosen in advance from the measured within-task spread.

| arm | n | median | sorted |
|---|---|---|---|
| shipped card | 10 | 209.28 | 102.2 144.2 169.7 179.6 193.7 224.9 227.4 232.8 262.0 276.7 |
| `--no-unteachable-rules` | 6 | 163.30 | 34.8 148.8 157.3 169.3 184.2 201.3 |

Exact one-sided rank-sum **p = 0.0589**, floor 0.000125. Control ranks 1, 4, 5, 6, 9, 11 of 16.

The p has not been converging on anything: 0.0804 at n = 3, 0.0529 at n = 4, 0.0589 at n = 6. That
is what a small effect looks like — and the size is measurable: **the medians differ by 1.28×**.
§83's power table puts n = 6 at 50 % against a 1.25× effect. So the experiment ran to its planned
floor and produced the outcome the plan said was a coin flip at that effect size. A null here is not
evidence of no effect; it is evidence that this arm, at this n, cannot see one this small.

**What it would take.** The same table gives 1.25× only 61 % detection at n = 8. Reaching 80 %
against 1.28× needs roughly fourteen to sixteen runs per arm — another eight to ten dollars on the
control side alone, on top of ten shipped-card runs that already exist. That is the honest price of
converting "not shown" into "shown or ruled out", and it should be a decision rather than a drift.

**What is settled.** The two clauses do not cost anything visible: five of the six control runs
shipped a compiled kernel (36–55 lines) exactly as all ten shipped-card runs did, and four of the
six land inside the shipped range. §88's guess that the arm might not compile is dead twice over.
The one outlier remains `remEEctl1` at 34.76 with a plain-Python champion, now clearly a single run
and not a pattern.

**What dissolved.** The process metrics that looked strong two sweeps ago (§87, §87.1) kept
weakening as runs arrived (§90) — dollars-to-first-node and minutes-to-first-build both walked back
toward 0.05. Nine statistics in this document have now moved against themselves; this arm produced
three of them, and the only reason each was caught the same afternoon is that `probe_summary.py`
prints them rather than a human retyping them.

### 91.1 The two runs, dissected

| | remEEctl5 | remEEctl6 |
|---|---|---|
| TEST | 157.2637 | 201.3289 |
| champion | 43 L kernel (`edge_cut.pyx`) | 36 L kernel (`_edge_expansion.pyx`) |
| nodes | 3 | 3 |
| eval_train | 23 | 32 |
| spend before first node | 29 % | 41 % |
| spend after last node | 5 % | 0 % |
| to first build | 24 m | 17 m |

Both `final.json` survived: these were the first two probes launched after the offset hazard was
fixed, and they are the evidence that the fix holds on the path that destroyed remEEctl1's and
remEEctl2's scores.

### 91.2 A seventh run, and the p walks away

`remEEctl7` finished twenty minutes after §91 was written: **233.5064**, a 52-line kernel, three
nodes, 25 % spent before the first node — the highest score the control arm has produced, above the
shipped card's own median of 209.28.

| n | control median | exact one-sided p |
|---|---|---|
| 3 | 169.34 | 0.0804 |
| 4 | 159.05 | 0.0529 |
| 6 | 163.30 | 0.0589 |
| **7** | **169.34** | **0.1349** |

Sorted, the seven are 34.8, 148.8, 157.3, 169.3, 184.2, 201.3, 233.5 against a shipped range of
102.2 – 276.7. Six of the seven sit inside that range; the seventh is `remEEctl1`, still the only
run that did not compile.

§91 said "not shown" and priced what showing it would cost. The seventh run says something narrower
and firmer: **the p is not converging, it is wandering, and the last step was away from
significance.** Four readings that walk 0.08 → 0.05 → 0.06 → 0.13 are the signature of a sample
drawn from one distribution, not two — which is exactly what a within-task spread of 2.7× produces
when the arms do not differ.

Buying the fourteen-to-sixteen runs §91 priced would be buying resolution on an effect the corpus
now gives no reason to expect. The honest recommendation is to stop this arm at seven and spend the
lanes on §83's next queued question instead, which is what they are now doing
(`--no-reference-affordance`, the arm §78 lost).

The section this corrects is twenty minutes old. It was not wrong — it reported n = 6 accurately and
hedged correctly — but it invited a reading ("needs more runs") that one more run undercut. That is
the ninth reversal in this document, and the first where the correction and the claim were written
in the same hour.

## 92. The card-batch arm closes at eight runs: no effect on the score, and the ranks say why

`remEEctl8` finished at 175.0618 — a 48-line kernel, three nodes, and a best-of 176.3262 against a
last node of 27.8174, the arm's sixth champion-rule save. That is the eighth and final run of
`--no-unteachable-rules`, the arm §83 put first in its queue.

| arm | n | median | sorted |
|---|---|---|---|
| shipped card | 10 | 209.28 | 102.2 … 276.7 |
| `--no-unteachable-rules` | 8 | 172.20 | 34.8 148.8 157.3 169.3 175.1 184.2 201.3 233.5 |

Exact one-sided rank-sum **p = 0.1185**, floor 0.0000229. Median ratio 1.22×. **Seven of the eight
control runs fall inside the shipped range**, and their ranks among all eighteen are
1, 4, 5, 6, 8, 10, 12, 16 — interleaved through the shipped distribution rather than shifted below
it. The one at rank 1 is `remEEctl1`, still the only run in either arm that shipped plain Python.

The p over the arm's life: 0.0804 (n=3), 0.0529 (n=4), 0.0589 (n=6), 0.1349 (n=7), 0.1185 (n=8).
It never crossed 0.05 and its last three steps went the wrong way. Interleaved ranks and a wandering
p are what one distribution looks like sampled twice.

**What this closes.** Removing the two clauses that experience never teaches — the per-instance
ceiling and "the best EVALUATED node is submitted" — does not measurably change what a run ships, on
this task, at this spread, for eight dollars. The process metrics that looked strong three sweeps ago
dissolved on the same schedule (§90). The arm answered its question; the answer is no.

**What this does NOT touch.** §84 measured the champion RULE at 17 of 24 runs saved and a 7×
difference in median submitted score. The rule is load-bearing; TELLING the model about it is what
shows nothing. Those are different claims and §84's is the one with p = 1/131072 behind it — six of
this arm's own eight runs are in that ledger, including remEEctl8's 6.34× and remEEctl4's 5.51×.
The arm removed the sentence, not the rule.

**Cost and verdict.** Eight dollars, eight runs, one clean negative on the question §83 ranked first.
That is what the plan was for: it named the sample size in advance, the arm ran to it, and the result
is reportable either way. The lanes have moved to §83's queue item 2 (`--no-reference-affordance`,
§78's lost control), where three runs are in flight.

## 93. The reference arm at n = 4: p = 0.043, and the two arms before it looked like this too

§83's queue item 2 is running. Four `--no-reference-affordance` probes have made `run_probe` calls,
so the pre-registered outcome — the share of those calls importing the reference — has its first
reading.

| card | n | median | sorted |
|---|---|---|---|
| shipped (pre-INSTRUMENT.txt) | 10 | 8.4 % | 2.7 6.2 6.7 7.1 7.4 9.5 10.0 10.7 10.8 20.0 |
| `--no-unteachable-rules` | 8 | 7.9 % | 2.8 6.9 7.0 7.7 8.1 9.7 11.1 12.5 |
| **`--no-reference-affordance`** | **4** | **2.1 %** | **0.0 0.0 4.2 10.0** |

Exact one-sided rank-sum against the shipped card, **p = 0.0430**. The unrelated control arm sits at
7.9 %, on top of the shipped 8.4 %, which is what a card change that should not move this dial looks
like — so the comparison has a negative control and it behaves.

**Two of the four never imported the reference at all.** That is not a lower rate, it is a different
behaviour, and it is what the removed clause literally offers. The CONTRACT sentence stays in the
card either way, so those runs knew the file existed and did not treat it as something to query.

**This is below the floor and I have watched this exact shape dissolve twice today.** §83 set n = 6;
this is n = 4. §87 read p = 0.0303 at n = 2, strengthened to 0.0070 at n = 4, and was back at 0.0276
by n = 5 with the minutes metric walking 0.0040 → 0.0492 (§90). §91 read the card-batch score at
n = 6 and the seventh run moved it from 0.0589 to 0.1349 (§91.2), closing at 0.1185 (§92). Three
arms, two of them already dead, and both looked at least this good at n = 4.

So this is recorded as a reading with its expiry stated: it means something at n = 6 with the same
sign, and nothing at all if it drifts the way the other two did. `remEEref4` is running and two more
lanes will free as the others finish.

The number is printed by `probe_summary.py` on every run, beside §69.1's band, and was made a
command BEFORE this arm had any data (67f774d7) — precisely so the reading could not be typed by
hand into a section written after the fact.

### 93.1 That p was computed over three unfinished runs, and the correction is not the one I expected

§93 reported the reference arm at n = 4, median 2.1 %, p = 0.0430. An hour later the same four
probes read 1.7 %, from nobody's edit. **Three of the four had not finished.** A rate on a running
probe counts only the `run_probe` calls it has made so far, and §93 compared one finished run plus
three partials against ten finished ones without saying so.

Finished-only, the arm is n = 1 at 3.3 %. There is no reading yet.

**The bias I assumed is not there.** I expected partial rates to run low — a model discovers the
reference module partway through, so an early sample should undercount. Measured over the 18
finished `edge_expansion` runs, the rate across the first fifteen `run_probe` calls has a median of
6.7 % against a final 7.8 %, and only **6 of 18 understate**; twelve overstate or tie. A partial rate
is NOISIER, not lower, over a denominator a third the size. So §93's number was not slanted toward
its conclusion — it was a median over two different precisions, which is a different fault and a
smaller one.

`probe_summary.py` now takes the median over completed runs only and prints the partials beside it
with their rate so far, the same split `after%` got. Hiding them would trade a silent mixture for a
silent omission; the mutation that hides them and the mutation that re-mixes them redden the same
two tests.

Two of the arm's own tests had to change with it: their fixtures never wrote a `champion_solver.py`,
so under the new rule every fixture probe was "running" and the median vanished. That is the code
being right and the tests being older than the distinction.

### 93.2 Finished-only, the reading is 0.1119 — and the mixture was doing the work

`remEEref1` and `remEEref3` finished, so the arm has three completed runs and the pre-registered
outcome can be read the way §93.1 said it had to be.

| | n | median | sorted |
|---|---|---|---|
| shipped card, reference use | 10 | 8.4 % | 2.7 … 20.0 |
| `--no-reference-affordance`, **finished only** | 3 | 3.3 % | 0.0 3.3 10.0 |

Exact one-sided **p = 0.1119**, floor 0.0035.

§93 quoted **0.0430** over one finished run and three partials. The finished-only number is two and a
half times larger. So the mixture was not merely imprecise, as §93.1 concluded from the bias
measurement — it produced a p the completed data does not support. §93.1 was right that partial
rates are noisier rather than lower, and wrong to treat "noisier" as the whole of the harm: three
partial denominators of 10-17 calls, resampled hours later, moved the median from 3.3 % to 1.7 %
and the p below 0.05. The correction to §93 is therefore stronger than §93.1 wrote, and this is that
correction.

**The score is untouched**, which is what this arm predicts and worth stating: control n = 3 at
median 205.58 against shipped 209.28, p = 0.5944. `remEEref3` scored **262.4246** — the second
highest number in the whole `edge_expansion` corpus, behind only remEE8's 276.73 — on a card that
never told it the reference was queryable. Both new champions are compiled kernels (44 and 34
lines).

Three runs is half of §83's floor. `remEEref4` and `remEEref5` are in flight and `remEEref6`,
`remEEref7` launched on the lanes these two freed.

### 93.3 Four finished runs, and the p lands back on 0.0430 — from a different set

`remEEref4` finished at 160.329 (62-line kernel). The arm now has four completed runs and the
pre-registered outcome reads, finished-only:

| run | reference use | `run_probe` calls |
|---|---|---|
| remEEref1 | 10.0 % | 20 |
| remEEref2 | 3.3 % | 30 |
| remEEref3 | **0.0 %** | 10 |
| remEEref4 | **0.0 %** | 18 |

Median 1.6 % against the shipped card's 8.4 % over ten. Exact one-sided **p = 0.0430**.

**That is the same number §93 printed, and it is not a confirmation of it.** §93's 0.0430 came from
one finished run plus three partials; §93.2 showed the finished-only set gave 0.1119; this 0.0430
comes from four finished runs, two of which imported the reference in *none* of their probes. Same
digits, different evidence, and a reader who sees 0.0430 twice will take the second for the first
being right. It was not — the intermediate reading is what the completed data supported at the time,
and both are below §83's floor of six.

Two runs at exactly 0.0 % is the part that is not a p-value. Those runs made 10 and 18 `run_probe`
calls and imported the reference in none of them, on a card that still tells them the file holds the
contract. That is the clause's own claim being visible in behaviour rather than in a rank statistic.

The score remains untouched: control n = 4 median 184.41 against shipped 209.28, p = 0.4196.

Four of six. `remEEref5`, `remEEref6`, `remEEref7` are in flight and `remEEref8` launched on the lane
remEEref4 freed.

### 93.4 Five finished runs, median 0.0 %, and this p is walking the other way

`remEEref5` finished at 153.8874 (38-line kernel, 4 nodes, 33 `eval_train`, 0 % spent after its last
node). Finished-only, the arm's pre-registered outcome:

| run | reference use | `run_probe` calls |
|---|---|---|
| remEEref1 | 10.0 % | 20 |
| remEEref2 | 3.3 % | 30 |
| remEEref3 | 0.0 % | 10 |
| remEEref4 | 0.0 % | 18 |
| remEEref5 | 0.0 % | 20 |

Median **0.0 %** against the shipped card's 8.4 %. Exact one-sided **p = 0.0140**, floor 0.00033.
**Three of the five imported the reference in none of their probes.**

The sequence over completed runs: 0.1119 (n = 3), 0.0430 (n = 4), 0.0140 (n = 5). **That is
monotone, and it is the first thing in this document that has been.** §87's dollars walked
0.0303 → 0.0070 → 0.0276, its minutes 0.0040 → 0.0492; §92's score walked
0.0804 → 0.0529 → 0.0589 → 0.1349 → 0.1185. Two arms wandered and died; this one is tightening in
one direction with each completed run, which is what a real effect does and what neither of the
others did.

It is still n = 5 against §83's floor of six, and `remEEref6`–`remEEref9` are in flight, so the floor
arrives on its own. The claim to make at six, if it holds, is narrow: the clause changes whether the
model treats the reference as something to QUERY, and nothing else visible. The score says so —
control n = 5 median 163.24 against shipped 209.28, p = 0.2970, no effect, exactly as this arm
predicts.

## 94. The reference arm crosses §83's floor with a result: p = 0.0021, and the score is untouched

`remEEref6` (220.4225, 36-line kernel) and `remEEref7` (272.1191, 46-line kernel) finished, taking
`--no-reference-affordance` to **seven completed runs** — past the floor §83 set in advance.

**The pre-registered outcome.** Share of `run_probe` calls importing the reference:

| run | rate | calls |
|---|---|---|
| remEEref1 | 10.0 % | 20 |
| remEEref2 | 3.3 % | 30 |
| remEEref3 | 0.0 % | 10 |
| remEEref4 | 0.0 % | 18 |
| remEEref5 | 0.0 % | 20 |
| remEEref6 | 0.0 % | 34 |
| remEEref7 | 0.0 % | 20 |

Median **0.0 %** against the shipped card's 8.4 % over ten. Exact one-sided **p = 0.00211**, floor
0.000051. **Five of the seven imported the reference in none of their probes** — `remEEref6` made
thirty-four `run_probe` calls and used it in none.

The sequence over completed runs: 0.1119 (n = 3), 0.0430 (n = 4), 0.0140 (n = 5), **0.00211**
(n = 7). Monotone the whole way, which neither of the two arms before it managed — §87's readings
walked 0.0303 → 0.0070 → 0.0276 and 0.0040 → 0.0492; §92's score walked
0.0804 → 0.0529 → 0.0589 → 0.1349 → 0.1185.

**The score is untouched, and that is the second half of the claim.** Control n = 7 median 205.58
against shipped 209.28, p = 0.4811, and **all seven land inside the shipped range** (102.2 – 276.7).
`remEEref7`'s 272.12 is the second-highest score in the whole `edge_expansion` corpus. Every one of
the seven shipped a compiled Cython kernel.

So the clause does exactly what it says and only that: telling the model `reference_<task>.py` is a
module it may QUERY changes whether it queries it, and changes nothing measurable about what it
ships. §78 said the acceptance test had lost its control group to the 2026-08-29 wipe and could not
be run "however many probes accumulate". It could, once something was built to remove the clause;
that took a flag, a byte-identity test and seven dollars.

**This is the first arm in the programme to answer its own question at its own planned n.** §92
closed with a clean negative; this closes with a positive on the outcome it named in advance and a
null on the outcome it predicted would not move. Both are results, and the difference between them
is the reason §83 insisted the outcome be named before the runs.

`remEEref8` and `remEEref9` are still running; they will make it nine, and they answer nothing new.
Under §83's own rule — a free lane takes a queued arm or stays idle — the two lanes those runs free
should NOT go to a tenth reference probe.

## 95. The quote §83 asked for: what re-measuring arm A would cost, and why the lanes are idle

`remEEref8` finished at 237.1105 (34-line kernel, best-of 238.7815 against a last node of 25.4787 —
a 9.37× champion-rule save), taking the reference arm to **eight** completed runs: median 0.0 %,
**p = 0.00103**, six of eight importing the reference in none of their probes. The arm answered its
question at n = 7 (§94) and a ninth run answers nothing new, so under §83's own rule — *a free lane
takes a queued arm or stays idle* — the three lanes it freed are not going to a tenth probe.

The next queued question is item 3, arm A against arm B: p = 0.75 over the ten comparable tasks, the
programme's actual question, and the one §83 said must be **priced before it is started**. Here is
the price, from the corpus rather than from an estimate.

**Arm A's own accounting is not the price.** docs/58 records it measured: *"12 of 12 scored arm-A
tasks exceed the $1.00 budget on their scoring attempt alone (100 % – 241 %); the six that exceed it
by ≥ 12 % are `pde_heat1d` 241 %, `sparse_eigenvectors_complex` 238 %, `rbf_interpolation` 201 %,
`rectanglepacking` 126 %, `kcenters` 125 %, `integer_factorization` 112 %."* The cause is named there
too — the arena does not count reasoning tokens.

**What it actually spent**, from §56: arm A's a3 attempt spent **$23.0073 over 2,225 calls =
$1.1504 per task**, against the $1.19 the comparison reports.

So re-measuring arm A on the ten tasks that have no number on the verified ruler:

| | |
|---|---|
| nominal, at the $1.00 the arena is told | $10 |
| at the measured per-task actual ($1.1504) | **≈ $11.50** |
| at the worst measured per-task overshoot (241 %) | up to **$24** |

And a known waste inside that: 5.2 % of arm A's budget bought 4.59 M completion tokens it was billed
for and never received, cut mid-stream on a precise 600-second period that is neither of the two
timeouts this bench controls.

**This is a spending decision and not a sweep task**, which is what §83 said about it in advance. The
lanes stay idle until it is made. Idle is the correct state for them: an unpaired run costs a dollar
and answers nothing, and that sentence has now survived three arms.

### 94.1 The arm closes at nine, and the monotone run ended on the last one

`remEEref9` finished at 218.8475 (26-line kernel, two nodes, 14 m to first build, and a tie in the
champion ledger). That is the last of the arm; every lane is now idle per §95.

**Reference use, all nine:** 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.3, 8.7, 10.0 — median **0.0 %** against
the shipped card's 8.4 %, exact one-sided **p = 0.00280**. Six of nine imported the reference in
none of their probes.

**§94 said the sequence was "monotone the whole way" and it stopped being so on the next run.**
0.1119 (n = 3), 0.0430, 0.0140, 0.00211, 0.00103 (n = 8), **0.00280** (n = 9). `remEEref9` used the
reference in 8.7 % of its probes — the second-highest rate in the arm — and pushed the p back up.
The claim held through eight runs and the ninth qualified it, which is the tenth time in this
document a sentence has been overtaken by its own next measurement. It does not change the verdict:
0.00280 is still two orders below the 0.05 the arm was sized for, and §83's floor was six.

**The score, all nine:** median **218.85** against the shipped 209.28 — now slightly ABOVE it —
p = 0.5789, and **nine of nine land inside the shipped range**. Nine of nine shipped a compiled
kernel. Whatever the clause does, it does not cost score, and the arm never suggested it would.

Final ledger of the two arms this document ran: `--no-unteachable-rules`, eight runs, no effect on
anything measured (§92); `--no-reference-affordance`, nine runs, a clear effect on the behaviour it
names and none on the score (§94). Seventeen dollars, two clean answers, both to questions written
down before the runs started.

## 96. The loop flagged its own best `discrete_log` node twice, and no sweep had looked

Zero of the last twenty-five commits touched `looplab/`. That is measurable and it is the drift this
document was warned about, so this sweep went looking for a defect in the LOOP rather than another
bench instrument. It found something better than a defect: a signal the engine has been raising for
days that no summary shows.

Counting event types rather than re-reading the ones already named turns up
`reward_hack_suspected` — **four events**, all the same signal:

    critic:params_ignored — "none of the proposed params [...] are referenced in the code"

All four are on `discrete_log`: **both** nodes of `remDL2`, and **both** nodes of `remDL7`. `remDL7`
scored **16.7799**, the best `discrete_log` number this bench has produced.

**This is not an engine defect.** Those runs carry `trust_gate: audit` — the shipped default, in
which a flag is advisory and the node stays eligible to win (`engine/options.py:184`,
`leakage.py:152`: *"under `trust_gate='audit'` (the default) these flags are advisory"*). The engine
did exactly what it was configured to do, and I checked that before writing this rather than after.

What was wrong is the reading. The standing brief calls `discrete_log` *"самое тонкое несущее число
корпуса"* — the corpus's finest load-bearing number, spread 5.1× — and nothing anywhere told a
reader that its best run had been flagged twice by the loop's own critic for writing code that
ignores every parameter the Researcher proposed. The diagnosis was in each run's event log, which is
the same place the zeros diagnosis and the refusal reason were hiding before they were surfaced.

`probe_summary.py` now prints the flags with their signal names and the sentence that they were
advisory. Two things are deliberately not done: the flags are not treated as disqualifying, because
under `audit` they are not; and no score is withdrawn, because a critic saying the params were
ignored is not evidence that the measurement was wrong.

### 96.1 What else the event census showed

`generation` is 0 on all 109 `node_evaluated` events and on every other event type that carries it.
It is the node's ATTEMPT number (`engine/ablation.py:34` compares `parent.attempt == generation`),
so this says **no evaluated node in the corpus was ever a repair** — every node that reached
evaluation did so on its first attempt. I first read it as a flat, non-deepening search and that was
wrong; measuring what the field means came before concluding, and the second reading is the dull one.

Also: 43 `novelty_rejected`, 155 `research_attempted`, 117 `card_build_attempted`, 44 event types and
16,782 events in total. Of those 44 types, this document has ever discussed about six.

## 97. The proposer's commonest second move is to propose its first move again

Continuing §96's census into the event types this document had never opened. `novelty_rejected`
fires **43 times across 35 of the 46 probes** — three quarters of every run this bench has done.

Every single one names `near_node` 0 or 1. The reasons are the loop's own words:

* *"Same experiment as node 0: identical algorithm (PH+BSGS discrete log), identical target (41-bit
  graded split), identical operator/params/space/eval pr…"*
* *"Node #0's graded submission is already an AOT-built Cython rho_dlog.pyx (setup.py build_ext,
  build ok, 3.2x) for discrete log in subgroup <g>; the pro…"*
* *"Same exact DST-I spectral solve as node 0; only delta is a m…"*

So: having produced a first node, the Researcher's next proposal is the first node again — a
reworded, re-parameterised, or differently-imported version of what it just did. The novelty check
catches it every time and returns `action: reproposed`.

**What it costs, measured.** Over $45.5997 of corpus spend: `propose` 22.4 % ($10.19), `repropose`
8.2 % ($3.75). Split by whether a run ever tripped the check:

| | n | median share of the run's dollar spent on `repropose` |
|---|---|---|
| probes that repeated themselves | 35 | **10.2 %** |
| probes that never did | 11 | **2.0 %** |

A factor of five, and about a twelfth of every dollar on the bench overall.

**This is not a defect and the distinction matters.** The novelty check exists for exactly this and
works every time it fires; nothing broken is being reported. What is being reported is a
characterisation nobody had written down: the loop's default second move is repetition, and the
guard against it is a recurring 8 % tax rather than a rare correction.

It also joins up with §84. After the first node the proposer either repeats it — 43 times, caught —
or produces something worse: 27 of 33 evaluated nodes that scored below the best so far were the
LAST node of their run. Repetition and regression are the two things that happen after node 0, and
the champion rule is what stands between the second one and the score.

`probe_summary.py` now prints the count on each probe's line, and only where it is non-zero: eleven
runs never did it, and a `0x` on every clean line is the noise that stops a line being read.

## 98. A skip took the last live copy of a claim, and the profiler kept saying the sentence anyway

`tests/test_algotune_profile_command.py::test_it_profiles_the_real_instance_at_the_graded_size` was
red on every sweep from 2026-08-30 and was closed on 09-01 by `needs_task("convex_hull")`, which is
the honest verdict: the task's 202 instance files never downloaded and the box is offline, so the
profiler cannot be exercised on it at all. The guard is right. What it did, though, is not neutral.

That test was the ONLY place asserting that the profile command feeds the REAL graded instance
rather than a toy — before this section `grep -rn 'graded size' tests/` returned two hits, and the
other one is a docstring in `test_algotune_correctness_check.py` about a different failure. So on the box where every score is computed, a
claim that went from RED to SILENT is now defended by nothing. **A skip that removes the last live
copy of a claim buys quiet, not safety.**

Four of the five tasks this campaign scores are cached here, with their sizes in the filenames:
`pagerank_T100ms_n4798_size100_train.jsonl`. `looplab_profile.py` parses no filename — it hands the
path to the arena's loader and calls `len()` on what comes back (`_describe`, line 90) — so those
digits are an oracle the tool cannot influence. That is the whole trick, and the reason to prefer
them over the profiler's own header:

```
profile: pagerank / train split / instance 0, the real graded size (loaded in 0.62s)
input: adjacency_list: list of 4798
```

**Mutation, 2026-09-02.** One line into `looplab_profile.py` — `problem["adjacency_list"][:100]` —
and the header still printed *"the real graded size"* while the instance was 48× smaller. Only the
`input:` line moved. The header is a claim; the length is evidence; a test that read the header
would have passed through the shrink it exists to catch.

`test_the_graded_size_is_still_checked_where_the_data_is_cached` asserts the `input:` line names the
number in the filename. It reddens under that mutation and passes clean. Commit below.

### The three snapshot claims I had only READ, now run

The standing list carried three items as *"НЕ ПРОВЕРЕНО мной"*. Reading the script is not measuring
it, so all three were executed:

| claim | measured |
|---|---|
| a snapshot whose destination vanished reports success | `SNAPSHOT_DEST=/no/such/mount/…` → **exit 1**, refuses before writing |
| nothing separates two concurrent snapshots | two launched together: **B done at 14 s, A at 36 s** — A waited on the `flock`; two distinct dirs, both `.complete`, 13 files and ~121 MB each, no interleaving |
| `.env` is neither copied nor named | `ENVIRONMENT.txt`, 32 lines, present in both, and it says so in as many words |

All three are closed. The first two were closed by earlier commits I had not exercised; that is
exactly the gap this table was for.

### And the money hint, measured live on the arm that ran after the fix

§67's table said the five roles that choose what to try next had never once been told the budget.
`plan` was closed on 08-31 through the Developer's own `_budget_note()`. The nine `remEEref` probes
of 09-01 are the first evidence, resolved through `span_input.py`:

| phase | prompts naming a money figure |
|---|---|
| `plan` | **210 / 210 = 100 %** |
| `plan_step` | 991 / 997 = 99.4 % |
| `deep_research` | 474 / 575 = 82.4 % |
| `propose` | 636 / 694 = 91.6 % |
| `repropose` | 154 / 169 = 91.1 % |
| `foresight_rank` | 0 / 87 — declined, 1.5 % of spend |
| `hyp_prioritize` | 0 / 75 — declined, 0.9 % of spend |

**The first version of that table was wrong, and the error was mine, in the instrument.** My
throwaway counter joined each message as `str(content)[:20000]`; the card is ~12 kB and the cue is
appended after it, so on `propose` and `repropose` the truncation cut off exactly the line being
counted. It printed `propose 22.9 %` and `repropose 0.0 %` — a clean, plausible, entirely false
story about a cue that had regressed. The committed tooling (`tests/test_the_proposer_sees_the_money.py`,
`plot_corpus.py`) does not truncate; nothing in the repo needed fixing. The lesson is the standing
one, one layer further in: **the instrument that checks the loop is itself an instrument, and a
number small enough to be believed is the one to re-derive with a second reading.**

The two card rules the list carries as unshipped — the 10× per-instance ceiling and "the best
EVALUATED solver is what gets submitted" — are both in the delivered card of every probe (`10x` twice
in the composed text, `KEEP_BEST` once), gated behind `--one-card --unteachable-rules`, which
`run_probe.sh:242` passes. Composing a card WITHOUT `--one-card`, as I first did, shows neither: the
absence was in my command line, not in the arm.

## 99. The cheap pre-flight command was answering about a path the grader never runs — both ways

Six runs in the corpus end on a node that scored 0. Two of them (`remEE6` node 3, `remEEref6`
node 2) died on a Cython `CompileError`. The obvious hypothesis is that the model skipped the cheap
command; **measured, it is false.** `check` is used heavily — 480 calls, 44.8 % of all dev-command
traffic, a median of 11 per run, and every probe but one uses it — and those two runs called it ten
and six times. Every call answered `"ok": true`.

The reason is in the solver:

```python
try:
    from edge_expansion_cy import edge_expansion_count
except ImportError:                    # <- the path `check` was validating
```

`_one_instance` imports `solver.py` in a child. Nothing had compiled the extension, so the guarded
import fell through and the checker certified the **pure-Python fallback**. `looplab_eval.py` then
ran `setup.py build_ext --inplace`, the compile failed, and the node was graded 0 — on a path the
checker never touched. That green light was the last thing the model saw before spending its final
draw.

**The mirror image was in the same command**, and the same experiment found it. The grader does
`sys.path.insert(0, str(code_dir))` before importing the candidate
(`AlgoTune/scripts/evaluate_results.py:396`), so a solver importing a sibling module or a compiled
extension is scored normally. The checker did not, and answered `ModuleNotFoundError` for every
instance: **13 of 480 `check` calls, across seven probes.** remEEref9's champion is exactly that
shape and scores **218.85** on the graded split while its own pre-flight command was calling it
invalid — a false RED steering a rewrite of working code, on the very affordance (`edit_surface`
grants `*.pyx`/`*.pxd`) that exists so the model CAN write more than one file.

One command, two verdicts, both about code the grader does not run:

| | before | after |
|---|---|---|
| broken `.pyx`, guarded fallback | `ok: true` (fallback) | `ok: false`, Cython's own line in `error` |
| helper module / compiled ext beside `solver.py` | `ModuleNotFoundError`, INVALID | valid, `build_ext: ok` |
| `.pyx` with no `setup.py` | silent | `build_ext:` "…so nothing was compiled and the pure-Python path was graded" |

`build_gate` imports the evaluator's own `build_decision` and `_build_error_digest` rather than
re-spelling them: a `.pyx` with no recipe is NOT an error (the evaluator grades the fallback and now
says so), and the compiler's line reaches the model in the same words both commands use. Two
spellings of one rule is how two commands come to disagree.

**It is cheap, which is why it belongs on the cheap command.** Measured 2026-09-02: the broken
`edge_expansion_cy.pyx` fails in **0.67 s** (Cython errors before the C compiler is reached), a
healthy `edge_cut.pyx` compiles in **1.3 s**, an unchanged rebuild is **0.4 s** — against this
checker's own 3.6–9.1 s and the card's 120 s ceiling for it.

Driven on the two real nodes: `remEE6` node 3 now returns `ok: false` with
`edge_expansion_cy.pyx:27:0: 'cpython/long/PyLong_AS_LONG.pxd' not found` in under a second, and
`remEEref9`'s champion — previously INVALID — now returns 2 of 2 valid with `build_ext: ok`.

Three falsifiers in `tests/test_check_sees_what_the_grader_will_run.py`, and both mutations redden:
removing `sys.path.insert` reddens the helper-module test, removing the `build_gate` call reddens
the compile test and the no-recipe test.

### And the archive comment that prescribed losing the other attempt

`snapshot.sh`'s own paragraph said **"THE BULK COPY IS `-n`, NOT `-u`"** while the code five lines
down is `cp -ru`. Extracted `archive_tree` and ran it twice over the same fixture — 400 archived
rows, `campaign.sh`'s `rm -rf`, then a 50-row second attempt:

| copy flag | canonical path | `.superseded-1` |
|---|---|---|
| `cp -ru` (the code) | attempt 2 (50) | attempt 1 (400) |
| `cp -rn` (the comment) | attempt 1 (400) | attempt 1 (400) |

Under `-n` the canonical path keeps attempt 1, the repair loop then sees a destination LONGER than
its source and leaves it alone by its own correct rule, and **attempt 2 is never archived at all** —
rc=0, `SUPERSEDED=1`, two copies of one attempt and a clean manifest row. The comment was right
about the danger and wrong about the cure; the preservation belongs in the `.superseded-N` loop,
which runs before any copy. Comment corrected against the measurement. Two existing tests already
hold the shape and both redden under `-n`, which is how the mismatch was confirmed rather than
argued.

**The standing list still carries this as open** — "closes only by versioning the archive per
attempt". Driven here, it is closed: `.superseded-N` IS that versioning, one layer down from where
the list looked for it.

## 100. The run's wall clock IS generation time, and the 300-second ceiling has now been measured on both sides

Two questions this corpus could always have answered and nobody had asked it. Both come off the
meter's `latency_ms`, which is stamped on all 13,329 metered generations.

### Where the clock goes

Sum every generation's latency inside one probe, divide by that probe's wall span:

| | |
|---|---|
| probes with a real span (>10 min, >20 calls) | 47 |
| **median share of wall clock spent inside LLM calls** | **103.2 %** |
| range | 87.9 % – 113.6 % |

Over 100 % is not an error: the loop overlaps some calls. But 103 % means the overlap is slight and
**the run is, to within measurement, doing nothing but waiting on the model.** Total across the
corpus: **111.8 hours inside generations.**

That is the missing half of §85, where twelve cut sessions were all cut by "time" and none by money
or nodes. The clock those sessions ran out of is generation time; nothing else is in it.

**And it re-prices every dev command.** `check` is 3.6–9.1 s, its new build gate 0.4–1.3 s (§99),
`eval_train` about 40 s. Against 111.8 hours of generation, the measuring commands are free — the
model is not trading clock for certainty when it runs one, it is trading a rounding error. `KEEP_BEST`
already tells it to "measure early, measure often"; this is the number behind that sentence, and it
argues for MORE measurement, not less.

### The ceiling, from both sides at once

The standing rule is that nginx's `proxy_read_timeout` measures the gap BETWEEN BYTES, so it only
fires on an unstreamed call. Split the whole ledger by `stream`:

| | calls | longer than 300 s | killed (504) | hours inside |
|---|---|---|---|---|
| streamed | 12,386 | 143 | **0** | 103.6 |
| unstreamed | 944 | 21 | **21** | 8.4 |

**Every unstreamed call that crossed the ceiling died — 21 of 21. Not one streamed call did, out of
143 that crossed it.** The longest surviving generation in the corpus is 1,820 s, six times the
ceiling. p99 of all generations is 303.3 s, sitting exactly on it.

Those 143 long streamed calls are 1.22 % of the traffic and **16.8 % of all generation time**. Since
the clock is generation time (above), that is 16.8 % of the campaign's wall clock riding on one
environment variable — the one I once switched off myself by handing the run someone else's `.env`.

### A note on the instrument, again

While checking the four live probes I read "no metered call for 652 s" off the ledger and nearly
wrote it up as a stall. The meter writes a row when a call RETURNS, so a generation in flight is
invisible to it by construction — and generations of 314 s, 500 s and 639 s completed in these same
four probes while I was looking. The second instrument settled it: `/proc/PID/wchan` said
`do_epoll_wait`, `state=S`, no children — waiting on a socket, which is what a long stream looks
like. Silence in a completion-stamped log is not evidence of a stall.

## 101. The four new probes are running at a third of corpus speed, and it is none of the things I suspected

`remDL9`–`remDL12` (discrete_log, one per lane, launched 09:04) are making **0.44–1.22 calls per
minute** against a corpus median of **2.34** — two to five times slower per probe. Three candidate
causes, all measurable, taken in order:

**Not the rate limiter.** `proxy.py` runs at `--rpm 45`. The four probes together are at **2.73
calls/min**, and `queued_s` is **0.00 s on every one of their 122 calls** — the limiter has not made
a single call wait. The corpus median `queued_s` is 0.00 s too.

**Not our own concurrency.** This one deserved a real measurement rather than a shrug, because
running four lanes into one shared endpoint is exactly the kind of thing that would slow itself
down. Every metered call bucketed by how many DISTINCT probes were calling within ±150 s of it:

| probes at once | calls | median tok/s |
|---|---|---|
| 1 | 304 | 104.9 |
| 2 | 2,504 | 112.2 |
| 3 | 3,512 | 100.1 |
| 4 | 6,993 | 101.8 |
| 5+ | 33 | 96.2 |

Flat from one lane to four. **Four-way occupancy is free**, which also settles that the lane layout
is not costing the campaign anything.

**It is the shared endpoint, right now.** Same task, same card, same box:

| | calls | median latency | mean latency | median completion tokens | **median tok/s** |
|---|---|---|---|---|---|
| the four live probes | 122 | 9.3 s | 108.7 s | 456 | **68.2** |
| corpus, discrete_log | 1,658 | 6.8 s | 59.3 s | 766 | **106.4** |
| corpus, everything else | 11,592 | 4.2 s | 25.7 s | 472 | **103.3** |

**68 tok/s against 103–106.** The answers are not longer — they are shorter. The communal model is
simply generating at about a third less throughput than it did for the corpus, and nothing on this
box is responsible.

### The obvious consequence does not exist, and that is the finding

§100 established that a run's wall clock IS generation time. The inference is immediate and
appealing: a third slower endpoint means a third fewer draws for the same run, so throughput is a
hidden term in every score this campaign reports. **Measured over 45 probes, it is not:**

| half | median tok/s | median evaluated nodes |
|---|---|---|
| slower | 92.8 | **3.0** |
| faster | 119.5 | **2.0** |

Spearman ρ = **−0.090**. No relationship, and what little there is points the wrong way.

The reason is one this notebook already recorded and I did not connect: **50 of 50 finished runs
end on `budget_exhausted`, none on any other reason** (§67's table, `max_eval_seconds` None on every
one). Money binds first, and money is priced per TOKEN, not per second. A slower endpoint changes
how long you wait for the same dollar's worth of tokens — not what the dollar buys. The wall clock
is generation time and the wall clock is not the constraint; both are true, and holding only the
first one makes a confident wrong prediction.

So: the four probes will finish, later than usual, for the same money. Nothing to fix.

## 102. The loop asks for a tool it will have later, and the refusal tells it nothing

First census of `result_is_error` over every tool span in the corpus — 18,212 calls:

| | calls | errors | |
|---|---|---|---|
| all tools | 18,212 | 426 | **2.3 %** |
| `run_probe` | 1,321 | 391 | 29.6 %, in all 46 probes |
| everything else | 16,891 | 35 | 0.2 % |

`run_probe`'s 30 % is not a defect and should not be read as one: it is the model's own scratch
script, and a script that raises is the tool working. (`exit=1 … stderr: Traceback` — the model
testing an idea and finding out.) The interesting 35 are elsewhere.

**36 of them are calls to tools that do not exist, and they are not random.**

| name | calls | probes | where that exact name DOES work |
|---|---|---|---|
| `write_file` | **31** | **20 of 46** | `plan_step` 391, `card_build` 14 |
| `read_memo` | 3 | 1 | nowhere |
| `run_probe` | 1 | 1 | `plan` 423, `plan_step` 879, `card_build` 18 |
| `python` | 1 | 1 | nowhere |

Every one of the 31 is `write_file` **in the `plan` phase**. The name is not a misspelling and the
tool is not missing — the Developer, while PLANNING, reaches for the tool it will have while
EXECUTING. Forty-three per cent of runs do it.

And the answer it got, every time, was the bare string `(unknown tool: write_file)`. That message
cannot correct the mistake: it does not say the name is right and the moment is wrong, it names
nothing that IS reachable, and there is no next action in it. Two of the four names in the table
(`read_memo`, `python`) look like exactly what follows — a model that got nothing to act on
guessing a second time.

`CompositeTools._unknown` now answers with the near neighbours from its own routing table and a
count of the rest, capped at five suggestions and under 200 characters, on both the string and the
typed dispatch path. Six falsifiers in `tests/test_an_unknown_tool_says_what_is_reachable.py`,
including one that pins an empty toolset back to the bare wording and one that guards a KNOWN tool
against being swallowed; reverting to the bare message reddens four of the six. Every other test in
the suite that asserts this message uses `in` or `startswith("(unknown tool")`, so the wording
change is compatible by construction — checked, not assumed.

**What this is worth.** 36 wasted turns is not much money. It is, however, the second time this
notebook has found the loop spending a turn on something the harness could have told it in one line
— the first was §99's `check`, which certified a code path the grader would not run. The pattern is
the same both times: **the loop is not told what it is allowed to do, only that what it did was
wrong.**

*Also censused and NOT a finding:* `repeat_streak` — 2.4 % of tool calls repeat the previous one
(414 at streak 2, 26 at streak 3, nothing above), all of them read-only (`read_code` 198,
`repo_read` 111, `web_fetch` 46). The cap already holds.

## 103. The one file the card tells the model to read is the one file its scratch tool could not import

Classified all 399 `run_probe` failures in the corpus by exception:

| | calls | share | probes |
|---|---|---|---|
| `ModuleNotFoundError` | **100** | 25.1 % | **39 of 46** |
| `TypeError` | 85 | 21.3 % | 28 |
| no exception (mostly `exit=-9`, the 60 s probe timeout) | 59 | 14.8 % | 32 |
| `IndexError` | 38 | 9.5 % | 18 |
| `ValueError` / `numba TypingError` | 30 / 30 | 7.5 % each | 17 / 19 |

Most of these are the tool working: a scratch script is a question, and a question that raises has
been answered. The top bucket is not. **94 of the 100 missing modules are the reference:**

| module | calls | probes |
|---|---|---|
| `reference_edge_expansion` | 43 | 21 |
| `reference_pde_heat1d` | 40 | 11 |
| `reference_discrete_log` | 11 | 7 |

`dev_probe.py::_replicate` materialised `staged.files` — what the model has WRITTEN. `reference_*.py`
is operator-planted and deliberately excluded from every submission: it is in
`repo_spec["protected_names"]`, and `campaign.sh` hands the scorer the same list as `--protect`. So
the file the card names as the thing to consult was precisely the file the scratch tool could not
see, and the launcher's own `sys.path.insert(0, os.getcwd())` cannot help a file that is not in the
cwd.

**This one is not just wasted turns.** Reference use is a MEASURED quantity in this campaign —
§69.1's 4.9–8.3 % baseline, and the nine-probe control arm of §94 that removed the affordance to see
what it was worth.

*Correction, measured the same day — see §105.* The sentence that stood here said the broken import
"suppresses the very number the arm was built to move". It does not. `probe_summary.py:316` counts
`(?:from|import)\s+reference_\w+` in the span text, i.e. ATTEMPTS, so a failed import is counted
exactly like a successful one and the 4.9–8.3 % band is intact. What the harness destroyed is not
the measurement but the VALUE of what it measured: 100 of the 104 in-probe imports returned nothing.
And the channel that dominates the affordance was never broken at all. §105 has the split.

`_replicate_given` now copies the task's protected files into the probe's disposable cwd under the
same caps, with **staged winning any name collision** — the model's own version is the truth for its
own build. The probe still cannot write, so this stays one-way.

Five falsifiers in `tests/test_the_probe_can_import_what_the_task_gave_it.py`. Three mutations, all
verified to redden: removing the call (the corpus failure, reproduced), letting the given file
shadow the staged one, and dropping the containment guard.

**The containment test caught me first.** Its initial version passed WITH the guard removed — I had
asserted that nothing appeared in `work/` and that the file outside was unmodified, and the escaping
write landed in a third place, `editable_path/outside/`, which neither assertion looked at. The
version that holds snapshots every path under `tmp_path` before the call and asserts the only new
ones are inside the disposable directory. A containment test that names the places it checks tests
the places it names; the mutation is what told me which those were.

## 104. A node graded on code written after its last `check` scores zero eleven times more often

§99 fixed what `check` measures. This is the other half: **whether the thing it measured is the
thing that got graded.**

Reconstructed from the tool spans of every evaluated node in the corpus — for each node, the last
`check` before its evaluation, and whether any file WRITE landed in the window between them:

| | nodes | scored zero | |
|---|---|---|---|
| a write landed after the last `check` | 12 | **4** | **33 %** |
| no write after it | 99 | 3 | **3.0 %** |

Exact one-sided Fisher **p = 0.0024**, over 111 nodes with a reconstructable order. Read the other
way: 4 of the 7 zero-scoring nodes were graded on code their own `check` had never seen, against 8 %
of the 104 non-zero ones.

The mechanism is not exotic and does not need one: the model checks, then edits once more — a
tidy-up, a rename, a "small" optimisation — and the evaluation grades the edit, not the checked
version. `remEEctl3`, `remEEref3`, `remEEref6` and `remPde4` are the four.

### Reported, not acted on

The obvious intervention is a line in the Developer's prompt: *you have edited since your last
`check`*. It is cheap — §100 priced `check` at 3.6–9.1 s against 111.8 hours of generation — and it
would plausibly convert some of those four into scores.

§92 is this notebook's standing answer to exactly that move. A behaviour change proposed off an
observational split has an unmeasurable effect until an arm exists that lacks it; the card-batch arm
closed at n=8 with p = 0.1185 and taught that lesson at the cost of eight probes. And the split here
is observational in a way worth naming: a model that edits after checking may be a model that is
already in trouble, in which case the edit is a symptom and removing it changes nothing.

So the quantity is now VISIBLE instead of acted on. `probe_summary.py` counts, per probe, how many
of its evaluated nodes were graded after a post-check write, and names them:

```
nodes graded on code written AFTER their last `check` (12 across 12 probes; ...):
  remDL4      1 of 2 evaluated node(s)
  remDL5      1 of 2 evaluated node(s)
  ...
```

Five falsifiers in `tests/test_the_summary_says_which_nodes_were_graded_unchecked.py`, and both
mutations redden: dropping the window (any write, any time) reddens three, and folding
"never checked at all" into the count reddens the test that keeps those two facts apart.

## 105. The reference affordance by channel: reading it always worked, running against it almost never did

§103 closed with a claim that does not survive its own instrument, and the instrument is one line:

```python
ref_imports = len(re.findall(r"(?:from|import)\s+reference_\w+", blob))   # probe_summary.py:316
```

It counts import STATEMENTS in the span text. A statement that raised `ModuleNotFoundError` is
counted exactly like one that worked, so §69.1's 4.9–8.3 % band and §94's control-arm comparison
measure **intent**, and the broken import did not move them by one point. The sentence claiming it
"suppresses the very number the arm was built to move" is wrong and is corrected in place above.

What is true is narrower and more interesting. Split the affordance by the channel the model
actually used:

| channel | uses | did it work? |
|---|---|---|
| `repo_read` / `read_file` of `reference_*.py` | **2,478** in 50 probes | always |
| `import reference_*` inside a `run_probe` script | 104 | **100 failed — 96 %** |
| `import reference_*` inside the graded `solver.py` | 3 of 112 nodes | yes (the file is in the node's own dir) |

**Reading the reference is the affordance; executing against it was a rounding error that mostly
failed.** Twenty-four uses of the working channel for every attempt at the broken one.

That is why §94 still stands: its arm removed the clause and watched reference use fall from a
median of 8.4 % to 0.0 % with the score untouched (p = 0.4811), and the traffic it was counting was
overwhelmingly the channel that works.

**And the model did not take the hint.** Of the 39 probes whose import failed, **31 kept trying** —
64 further attempts after the first failure, only 8 gave up. An affordance that fails silently is
tried again; it is `(unknown tool: write_file)` in another costume (§102), and the same fix applies:
say what is reachable. §103's `_replicate_given` now makes it reachable instead.

### The correction that matters more than the number

Yesterday I wrote a sentence about a metric without reading the line that computes it, and it read
as the strongest form of the finding. Both halves of this sweep's method note apply at once: the
measurement was right and the sentence around it was not, and the only thing that caught it was
opening `probe_summary.py:316` to ask what `ref_imports` actually counts.

## 106. The money tool read its two halves in the wrong order, and its graceful path was the one that crashed

**The first non-zero residue of the campaign, and it was negative.** With four probes live this
sweep reported `RESIDUE $-0.003329` — about the price of one generation. A leak does not point that
way: negative means the SPANS held more than the counter, i.e. money the engine recorded and the
meter had not.

Re-run seconds later: `$-0.000000`. That is the signature of a race, not of a leak, and the race is
in `check_money.py` itself:

```python
live = _counter(a.port)                              # counter snapshot
s_cost, s_calls = spans_by_probe(a.bench_root, since)   # ...then the slow glob over every probe tree
```

The counter was sampled first and the spans second, so any call that COMPLETED between the two reads
was in the span sum and not in the counter. The file's own header states the opposite rule —
*"sum `attributes.cost` over `generation` spans, then read the counter"* — which is also what the
standing brief prescribes on every sweep. **The rule was documented at the top of the file and
inverted twenty lines down.**

Spans first, counter second: the counter can then only have GAINED between the reads, the gap is
non-negative by construction, and what remains in it is what the named parts explain. Reading the
spans is the slow half, which is exactly why it must go first.

### And the test found a second one on its way in

The order test points the tool at a root that does not exist. It died before reaching its assertion:

```
ValueError: not enough values to unpack (expected 4, got 3)   check_money.py:191
```

`meter_by_probe` returns four dicts everywhere except its missing-file branch, which returns three.
So the one path built to be graceful — no meter log yet, a fresh `BENCH_ROOT`, a mistyped
`--bench-root` — was the only one that crashed, and it crashed in the tool the sweep uses to decide
whether money is intact. No earlier test had ever pointed it at an absent root.

Both fixed, both with falsifiers: reverting the order reddens the order test, and restoring the
three-tuple reddens two.

**The shape, for the third time this week:** §99's `check` certified a path the grader would not
run, §103's probe could not import the file its card names, and now the reconciler samples its two
sides in an order its own docstring forbids. None of these is a broken component. Each is a
component that measured something adjacent to what it claimed.

## 107. The train number predicts the graded number to within a few per cent, over 44 probes

The card's sharpest warning is about the hidden split:

> THE REPORTED SCORE IS ON A SPLIT YOU CANNOT SEE. Train is what you tune against; the champion is
> finally scored on held-out instances from the same generator. So anything that fits the train
> instances SPECIFICALLY -- a lookup table, a hard-coded answer, a threshold tuned to one of them --
> scores zero where it counts.

Nobody had ever asked the corpus whether that happens. Best train metric against the graded TEST
score, every finished probe that has both:

| | n | median TEST/train | range |
|---|---|---|---|
| **all** | **44** | **0.998** | 0.963 – 1.260 |
| edge_expansion | 27 | 0.993 | 0.963 – 1.015 |
| pde_heat1d | 10 | 1.022 | 0.991 – 1.054 |
| discrete_log | 7 | 1.007 | 0.980 – 1.260 |

*Correction, one sweep later — see §111.* Pooling these three was wrong. Four more `discrete_log`
runs finished on 2026-09-02 and took THAT task's band to 37 percentage points (0.890 to 1.260),
while `edge_expansion` stayed inside 5.2 and `pde_heat1d` inside 6.3. The pooled figure below is a
median of three different questions; the per-task table is now what `probe_summary` prints.

**Forty-two of the forty-four land inside ±3.5 %.** The worst loss in the whole corpus is
`remEEctl4` at 0.963 — 3.7 % — and the one figure above 1.06 is `remDL6`, whose absolutes are small
(3.201 → 4.033) and where a single instance moves the ratio. Twenty-four of 44 land below 1.0, all
by tenths of a per cent: a small, systematic, task-shaped offset, not a cliff.

**So the warning describes something this corpus does not do.** Not one run gained on train and
collapsed on test, which is what train-specific fitting would look like and is exactly what the
per-task band would expose. `eval_train` is a trustworthy proxy for the number the campaign reports.

That is worth stating plainly because it removes a candidate explanation for the thing this
programme is actually stuck on. The spread between runs on one task is 2.7× on `edge_expansion` and
4.2× on `discrete_log` (§101, §83). It is not measurement noise: the ruler agrees with itself to
1.6 % (the reference-against-itself checks) and now the train half agrees with the graded half to a
few per cent. **The variance is in which solver the loop writes, not in how it is scored** — which
is where §84's champion rule and §97's repeating proposer already pointed.

`probe_summary.py` prints the band with both endpoints NAMED on every sweep, because the claim is
the spread and a median alone would hide the one run that broke it. Three falsifiers; printing only
the median reddens the test that plants an overfitted run at 0.500 and demands it be named, and
lowering the five-probe floor reddens the test that refuses to call four points a band.

## 108. What the loop is missing: the score is one binary choice, and the loop walks away from it on a coin flip

Asked the corpus the question the programme is actually about — why a dollar buys the score it buys.

### The dollar

| | median share of the run |
|---|---|
| before the first evaluated node | 37.9 % |
| each step between nodes | 32.6 % (n = 64 transitions) |
| after the last node | 3.6 % |

Node counts: 12 runs got one, 14 got two, 19 got three, 4 got four. So a dollar buys **two to three
draws**, and the first one costs nearly two fifths of it.

### The draws are not a refinement sequence

Running maximum, normalised to the first node:

| task | after 1 | after 2 | after 3 | after 4 |
|---|---|---|---|---|
| edge_expansion | 1.00× | **5.17×** (n=27) | 6.93× (n=21) | 8.33× (n=4) |
| discrete_log | 1.00× | 1.00× (n=5) | — | — |
| pde_heat1d | 1.00× | 1.00× (n=5) | — | — |

On two of the three tasks **no later node ever beats the first one.** On the third the second node is
worth five times the first — and then look at the actual sequences:

```
remEE3     [28.1, 194.7, 20.5]          remEEctl4  [154.4, 26.7, 28.0]
remEE4     [178.9, 265.8, 13.0]         remEEctl7  [25.4, 162.0, 238.4]
remEE5     [27.2, 27.9, 232.5]          remEEref3  [22.8, 267.7, 22.4, 0.0]
remEE6     [22.6, 234.9, 148.7, 0.0]    remEEref5  [25.2, 157.4, 28.3, 23.1]
```

Two clusters, ~20–30 and ~150–280, and the run hops between them. That is not refinement; it is
sampling from a bimodal distribution, which is why §84's champion rule carries so much weight
(p = 7.45e-09) and why "more nodes" barely moves the final score.

### The two clusters are one binary choice

| edge_expansion, per node | n | median | max |
|---|---|---|---|
| Cython `.pyx` kernel | 42 | **166.49** | 277.23 |
| numba | 14 | 27.52 | 28.33 |
| pure Python | 23 | 22.86 | 35.02 |

**Six-fold, and it is the whole spread.** `discrete_log` points the same way with a smaller gap
(Cython 10.75 against numba 7.04). The score of a run is very nearly the answer to one question:
did it write a compiled kernel.

*Correction, measured the next sweep — see §110.* That last sentence is true of `edge_expansion`
and of nothing else. Read at CHAMPION level rather than node level: 26 of 27 `edge_expansion`
champions carry a kernel and the one that does not is last by a factor of three, but on
`discrete_log` the kernel is neither necessary nor sufficient, and on `pde_heat1d` not one of the
ten champions has one while the spread there is still 5.5×. `edge_expansion` is 27 of the corpus's
49 runs, which is why the pooled view reads as a general law.

### Two things the loop does not do

**1. It does not stay on the regime it found.** Of the 28 transitions in `edge_expansion` that
START from a kernel node, **14 propose a kernel again and 14 propose something else** — a coin flip
away from a 166× median toward a 25× one. `remEEctl4` is the clean case: 154.4, then 26.7, then
28.0. §97 measured the other half of the same behaviour — 43 novelty rejections in 35 of 46 probes,
every one naming node 0 or 1 — so the second move is either a literal repeat of the first or a
regression to the other cluster. **There is no exploitation phase.**

**2. The finding never leaves the task.** Kernel adoption, per task:

| task | runs with an evaluated node | runs that ever wrote a `.pyx` |
|---|---|---|
| edge_expansion | 27 | **26 (96 %)** |
| discrete_log | 11 | 5 (45 %) |
| pde_heat1d | 11 | **0 (0 %)** |

And in the shared lesson store: **0 of 60 statements mention Cython, `.pyx`, a compiled kernel or
`build_ext`.** The single highest-leverage fact this benchmark has produced has never been distilled
into a lesson, so there is nothing to carry it to the task where it was never tried.

Whether a kernel would help `pde_heat1d` is NOT established here — numba reaches 161.55 there and
that may be near the ceiling. What is established is the asymmetry: 42 kernel nodes on one task,
6 on another, 0 on the third, with nothing in memory that would explain the difference to the next
run.

### So the answer to "what is missing"

Not measurement: the ruler agrees with itself to 1.6 % and train predicts test to a few per cent
(§107). Not throughput: endpoint speed buys no nodes (§101). Not the budget's size: 57 % of it goes
to planning at every budget (§65's correction). **What is missing is memory of a win and the will to
stay on it.** The loop finds the 166× regime, records nothing about it, and spends its next third of
a dollar proposing either the same node again or a 25× one.

Both halves are testable with arms this bench can already build, and neither is shipped on the
strength of this section — §92 is the standing rule. The queue entry that follows from it is a
control arm on ONE clause: *when a node scored well, propose a variant OF IT.*

## 109. §108's finding, built as an arm instead of switched on

The clause §108 ends on — *when a version scored well, the next one should be a variant of it* — is
the most obviously right thing this notebook has proposed all week, which is exactly why it is not
shipped on.

`make_task.py --exploit-best`, **OFF by default**, and the default is the whole point:

| arm | card | n |
|---|---|---|
| control | the shipped card, unchanged | **49 runs, already paid for** |
| treatment | `--exploit-best` | to be run |

Leaving it off keeps the shipped card byte-identical to the one those 49 probes ran on, which makes
the corpus the control group at zero cost. A clause that arrived ON by default would have retired
all forty-nine in one commit — the mistake §78 records the corpus already paying for once.

The clause carries the measurement rather than the advice:

> AND WHEN SOMETHING WORKS, THE NEXT THING YOU TRY SHOULD BE A VARIANT OF IT. […] on
> `edge_expansion` a solver with a compiled Cython kernel scores a median 166x and everything else
> — numba, pure Python — a median 26x […] Of the 28 times a run stood on a kernel node and proposed
> again, 14 proposed a kernel and 14 proposed something else […] A second idea that shares nothing
> with the first is a fresh draw from the same distribution, not progress.

It sits under `--one-card` beside `KEEP_BEST` because the two are one subject from two sides:
`KEEP_BEST` says a WORSE attempt costs nothing, this says an UNRELATED attempt costs a draw.

Four falsifiers, and both mutations redden: flipping the default to ON reddens the test that keeps
the shipped card clean, and making the flag also suppress `KEEP_BEST` reddens the test that the two
arms differ in exactly one clause — the property that made §92 and §94 readable at all.

**What it would cost to answer.** §83's power table: n = 6 per arm gives 50 % power against a 1.25×
effect, n = 9 is what the reference arm needed to close. The control side is free. So the price of
the answer is **9 probes ≈ $10.35** at the measured $1.1504 — and `edge_expansion` is the task to
run it on, because it is the only one of the three where later nodes beat the first at all (§108),
i.e. the only one where "propose a variant" has anything to act on.

## 110. §108's binary choice is one task's story, not the corpus's

§108 said the score is very nearly the answer to "did it write a compiled kernel". Checked at
CHAMPION level — the solver that was actually submitted, per run — that holds on one task out of
three.

| task | champions | with a kernel | spread of TEST |
|---|---|---|---|
| edge_expansion | 27 | **26** | 34.76 – 276.73 |
| discrete_log | 10 | 4 | 4.03 – 16.78 |
| pde_heat1d | 10 | **0** | 30.33 – 167.21 |

**edge_expansion — the claim survives and gets sharper.** The single champion without a kernel is
`remEEctl1` at **34.76**, against a next-worst of 102.17 and a median of 193.67. One run out of
twenty-seven failed to write a kernel and it lost by a factor of three to everyone else.

**discrete_log — the kernel is neither necessary nor sufficient.** The top three champions have one
(16.78, 14.05, 12.40), but 12.18 and 8.10 do not, and the run at the very bottom — `remDL6` at
**4.03** — does. Whatever separates 16.78 from 4.03 here, it is not that choice.

**pde_heat1d — nothing this corpus can see.** Not one champion in ten uses Cython, every one uses
`njit`, and the spread is still 5.5× (30.33 to 167.21). Scanning the champions for coarse
algorithmic markers separates nothing: `dst` appears in 3 of the 7 above 115 and 1 of the 3 below,
`np.linalg` in 3 and 2, `fastmath` in 2 and 0. **The 5.5× is unexplained**, and saying so is the
honest state of it.

`edge_expansion` is 27 of the corpus's 49 runs, so a pooled count is dominated by the one task where
the effect is real — which is exactly how §108 came to state it as a general law.

### And a hypothesis of my own, killed on arrival

The finished `discrete_log` probes made a pattern look obvious in the summary's champion column:
16.78 with a 26-line kernel, 14.05 with 41, 12.40 with 16 — against 240–324 lines further down. A
short champion looked like a good champion. Measured over every run, Spearman between champion size
and TEST is **+0.091** (discrete_log), **+0.181** (edge_expansion), **+0.139** (pde_heat1d): no
relationship, and all three the opposite sign from the guess. Two numbers in a column that were
already sorted by score is not a pattern; it is the column being sorted.

**Nothing follows for §109's arm.** That clause is about proposing a VARIANT of what worked, and its
evidence is the transition count — 14 of 28 proposals from a kernel node walk away from the kernel
— which is a fact about `edge_expansion`, the task §109 already names as the one to run it on.

## 111. The train number predicts the graded one on two tasks out of three

§107 read one band off 44 probes. The four `discrete_log` runs that finished today take that task to
n = 11 and break the band:

| task | n | median | low | high | spread |
|---|---|---|---|---|---|
| edge_expansion | 27 | 0.993 | 0.963 (remEEctl4) | 1.015 (accEE) | **5.2 pp** |
| pde_heat1d | 10 | 1.023 | 0.991 (remPde6) | 1.054 (remPde3) | **6.3 pp** |
| **discrete_log** | **11** | 1.001 | **0.890** (remDL10) | **1.260** (remDL6) | **37.0 pp** |

`discrete_log` sorted: 0.890, 0.980, 0.983, 0.993, 0.998, 1.001, 1.007, 1.008, 1.018, 1.054, 1.260.
The middle nine are as tight as the other tasks; both ends are far outside them, and both ends are
`discrete_log`.

**This matters for what the corpus is used to argue.** The standing brief calls `discrete_log` "the
corpus's finest load-bearing number", and it now carries two independent sources of noise, not one:
a between-run spread of 4.2× (§101) AND a train-to-test disagreement of up to 37 points on the SAME
solver. A conclusion drawn on `discrete_log` needs both.

§107's headline survives where it was measured — on `edge_expansion`, 27 runs inside 5.2 points, no
run gains on train and collapses on test. It does not generalise, and the pooled band was hiding
which task it came from. Corrected in place; `probe_summary.py` prints the band per task now.

### The test I wrote to pin it picked the wrong line

The first version selected the band's rows with `startswith("discrete_log")` — and the by-card spend
block prints lines starting with `discrete_log` too, so the assertion read
`discrete_log unrecorded (pre-INSTRUMENT.txt) n= 5 median $0.0000`. The fix anchors on the block's
HEADER and stops at the blank line. That is the same substring-anchor mistake this notebook has now
recorded four times, and the only reason it did not survive is that the fixture made the wrong line
obviously wrong.

## 112. The control group ran on a different card, and the arm was three minutes old when that turned up

Launched §109's arm on the four free lanes — four `edge_expansion` probes with `--exploit-best` —
and then went looking for the control group's card fingerprint. It is not recorded: the ten shipped-
card `edge_expansion` runs predate `INSTRUMENT.txt` entirely, and the four that have one
(`remEE6`–`remEE9`) were written by 4a1c6940, the commit that FIRST added the file, before it
carried `card_args` or `card_sha256`.

So the control card had to be reconstructed. `git show 4a1c6940:benchmarks/algotune/make_task.py`,
run against the same checkout:

| | sha of `goal` | length |
|---|---|---|
| shipped card, as the control ran it | `8043dd2df7162322` | 16,735 |
| shipped card, today | `24a3d7803af799c2` | 16,797 |

**RETRACTED — see §113.** They are the same card. The reconstruction ran a COPY of `make_task.py`
from a scratch directory, and `BASELINE_TIMES_DIR` resolves against `Path(__file__).resolve().parent`
— so the copy could not find `.baseline_times` and silently built the fallback wording. Today's
script, run from the same scratch directory, produces `8043dd2df7162322` too. Rebuilding the card at
all 24 recorded commits gives that one sha every time. The control group was valid and the four
probes were stopped on an artifact of my own instrument. What follows below is the diff between a
card WITH this box's timings and one without, not a diff between then and now:

```
- The dataset's name says the reference took about 100 ms per instance ON THE MACHINE THAT BUILT
  IT -- nothing here has measured this box, so treat that as an order of magnitude…
+ THE REFERENCE COSTS **46 ms** PER INSTANCE ON THIS BOX -- the median of the per-instance
  reference timings the scorer itself divides by…
```

The control arm was told 100 ms and the treatment arm would have been told 46 ms. That is the exact
property §92 and §94 were readable BECAUSE of — every other byte identical — and it would have been
violated silently, in a comparison costing about ten dollars, by a corpus that looked free.

Four probes stopped at three minutes, **$0.0579 spent**, trees removed. Relaunched as a paired
design on the same four lanes: **two treatment (`expEEa`, `expEEb`) and two control (`ctlEEa`,
`ctlEEb`), both cards built by today's code, running at the same time on the same box.** The old
corpus is no longer the control group; it is background.

### And the stop produced a defect worth keeping

`check_money.py` immediately reported `RESIDUE $+0.057925` and exited 1. The money was never in
doubt — it was the four stopped probes, whose meter rows outlived their trees — but the tool could
not say so, and the standing brief carries this case as a MANUAL step: *the counter also counts the
abandoned probe, so add it to the live sum by hand or you get a false discrepancy.* A step an
operator must remember is a step that gets forgotten.

The signature needs no list to maintain: **an arm the meter knows and the probe trees do not.**
`run_probe.sh` writes the tree before the first call, so "meter rows, no tree" cannot be a running
probe. `check_money` now names them:

```
38 call(s) from 4 ABANDONED probe(s) -- in the meter, no tree on disk:
   expEE1 $0.0114, expEE2 $0.0148, expEE3 $0.0156, expEE4 $0.0162
RESIDUE $-0.000007 after the named parts
```

Two falsifiers; dropping the subtraction reddens the first, and a probe WITH a tree is never called
abandoned however its calls line up.

## 113. The card I thought had changed was the same card; my copy of the script could not find the timings

§112 stopped a four-probe arm on the finding that the shipped card had changed since the control
group ran. **It had not.** Rebuilding the `edge_expansion` card at every one of the 24 commits any
probe records:

```
f4258573 8043dd2df7162322 16735     ea2f9a6a 8043dd2df7162322 16735
33368329 8043dd2df7162322 16735     2cb7b965 8043dd2df7162322 16735
…  (24 commits, one sha, one length)
```

One card, unchanged across the whole corpus — including `f4258573`, which is this morning's HEAD and
which yesterday I had compared AGAINST and found different.

The difference was in how I ran it. `make_task.py` resolves its timings as

```python
BASELINE_TIMES_DIR = Path(os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR")
                          or (Path(__file__).resolve().parent / ".baseline_times"))
```

and my reconstruction copied the script into a scratch directory, where that path does not exist.
Without the timings the card states the dataset name's target — *"about 100 ms … an order of
magnitude"* — and with them it states the measured one — *"THE REFERENCE COSTS 46 ms PER INSTANCE
ON THIS BOX"*. Running TODAY's script from the same scratch directory reproduces the "old" card
exactly: `8043dd2df7162322`, 16,735 characters.

So the diff §112 printed is real, and it is a diff between **a card built beside its timings and a
card built away from them** — not between then and now. Retracted in place.

### What the mistake cost, and what it bought

Cost: four probes stopped at three minutes, **$0.0579**, and a relaunch. The relaunch is not itself a
loss — a control arm running concurrently on the same box is a better design than a historical one,
and the old ten-run control is now additional evidence rather than the only evidence.

Bought, and worth more than that: **the card silently becomes a different card when the timings are
missing, and nothing said so.** A probe built on a box without `.baseline_times`, or with
`ALGOTUNE_BASELINE_CACHE_DIR` pointing anywhere wrong, ships the vague clause and scores against a
model that was told a number 2.2× too large. `make_task.py` now prints to stderr, naming the
directory it looked in and what the fallback does; `run_probe.sh` puts that in the probe's log.
Three falsifiers, and removing the print reddens the first.

**The instrument that lied was mine, again, and the shape is the one this notebook keeps writing
down.** A script is not a pure function of its source: `make_task.py` reads a directory beside
itself, so a copy of it is a different program. I checked what the code said and not what the
program did, and it took a $10 decision with it. What caught it was the boring version of the same
measurement — rebuilding all 24 commits instead of one, and seeing every single one agree.

## 114. Every probe launched today wrote a compiled kernel as its FIRST node, and neither arm explains it

The §109 arm's four probes reached their first evaluated node. Both arms, side by side:

| probe | arm | node 0 |
|---|---|---|
| expEEb | `--exploit-best` | **269.31** |
| expEEa | `--exploit-best` | **250.11** |
| ctlEEa | shipped card | **172.93** |
| ctlEEb | shipped card | **159.79** |

Against the corpus's 27 `edge_expansion` first nodes:

```
20.5 20.8 21.6 22.6 22.8 22.8 22.9 22.9 23.7 25.2 25.4 27.2 27.5 27.7 27.8 28.1
31.9 35.0 52.6 59.8 | 132.7 141.7 150.7 154.4 154.4 159.5 178.9
```

Median **27.7**, and exactly one of the 27 reaches today's lowest. All four of today's land above
it. One-sided permutation test on ranks: **p = 0.000127**.

*Updated the same evening, at six first nodes — see §118.* `ctlEEc` came in at **20.57**, squarely
in the low cluster, so it is 5 of 6 above 150 rather than 4 of 4, and the permutation p moves to
**0.005673**. The effect survives and is smaller than four points made it look.

**It is not the clause under test.** Both control probes show it, and §109's clause is about the
SECOND proposal — there is nothing to make a variant OF when node 0 is proposed. Whatever moved,
moved for both arms.

### The mechanism is visible in the probes

| | corpus | today |
|---|---|---|
| node 0 carries a Cython kernel | 9 of 27 | **4 of 4** (Fisher one-sided p = 0.023) |
| `check` calls whose answer mentions `build_ext` | **0** | 10 of 17 |
| reference imports inside `run_probe` that worked | 4 of 104 | 2 of 3 |

In the corpus the kernel arrives at node 1 in 15 runs and node 2 in 2 more — the loop got there on
its second or third draw. Today it starts there. And the three harness repairs shipped this week are
exactly the ones that would do that:

* **§99** — `check` now runs the evaluator's own `build_ext` and puts the submission's directory on
  `sys.path`. Before, a Cython solver's guarded import fell through to pure Python and `check`
  certified the fallback; a multi-file solver came back `ModuleNotFoundError` and read as INVALID.
  The cheap command could not validate a kernel at all. Now it does, in 0.4–1.3 s.
* **§103** — `run_probe` replicates the operator-given `reference_*.py`, so a scratch script can
  import it. 94 of the corpus's 100 `ModuleNotFoundError`s were exactly that import.
* **§102** — an unknown tool name is answered with what IS reachable.

**This is the first thing in this programme that plausibly moves the score, and it is not a card
clause.** It is the pre-flight tool being able to see the code path the grader runs.

### What it does to the arm

`n = 4`, two per arm, so nothing here is a result about `--exploit-best` and nothing is claimed.
What it does mean is that **the 27-run corpus is no longer background for this arm**: those runs
carry the old `check`, and today's carry the new one. §109's design already had the answer — its
control runs concurrently, on the same box, on the same harness — so the arm is still readable
within itself. The historical comparison is not.

And the same fact is the strongest available argument for a proper measurement of the harness
repairs themselves: this is `n = 4` with `p = 0.000127` on ranks and a mechanism visible in three
independent counters, which is exactly the shape that deserves an arm rather than a paragraph.

## 115. The loop did not work harder for that first node — it did the same work and got a different answer

§114 measured the outcome. This is the effort behind it, on the same 27-versus-4 split, counting
only what happened BEFORE the first evaluated node:

| before node 0 | corpus (n=27) | today (n=4) |
|---|---|---|
| `check` calls | median 5.0 (2–8) | 4.5 (3–5) |
| `eval_train` calls | 4.0 (2–15) | 4.5 (2–5) |
| `run_probe` calls | 9.0 (2–27) | 10.5 (6–20) |
| **money spent** | **$0.3208** ($0.2366–$0.5331) | **$0.2910** ($0.2327–$0.3289) |

**Indistinguishable, and slightly cheaper.** The loop is not measuring more, planning longer or
spending more to arrive at a compiled kernel on its first draw. It runs the same commands the same
number of times; what changed is what those commands ANSWER. §114's counter is the one that moved:
`build_ext` appears in 10 of 17 `check` answers today and in 0 of the corpus's.

That is the sharpest form of the §99/§103 claim. The repairs did not give the loop a new capability
or a new instruction — they made the capability it already had report on the code the grader runs.

### The confound I cannot rule out from here

Everything in §114 and above compares runs from different DAYS, and the model on the other end is a
shared endpoint whose behaviour is not under this bench's control — §101 already measured its
throughput moving by a third between the corpus and today. Nothing here separates "our pre-flight
repairs changed the first draw" from "the endpoint changed".

The arm that would separate them is cheap and specific: **two probes today on the OLD `check`** —
the pre-§99 `looplab_check.py`, everything else identical. If the first node comes back at ~27 the
repair is the cause; if it comes back at ~200 the endpoint is. That is 2 probes, about $2.30, and it
is the first item this notebook has queued whose answer would change what we believe about our own
work rather than about the loop's.

Not run now: all four lanes are on §109's arm, and taking one down mid-arm to answer a different
question is how §112 already cost $0.06.

## 116. The first probe of §109's arm to finish, and an impression that did not survive being measured

`ctlEEb` (control card) finished: **TEST 156.9089** against a best train of 159.7902 — ratio 0.982,
inside `edge_expansion`'s 0.963–1.015 band (§111). Champion is a 48-line Cython kernel; $1.0108;
24 % of the dollar before the first node and 0 % after the last; 28 `eval_train` calls; reference
use 9.5 % import / 9.5 % `is_solution`, above §69.1's 4.9–8.3 % band. Money: `propose` 31.3 %,
`plan_step` 29.0 %, `repropose` 16.3 %, `deep_research` 12.2 %. Its nodes were
**[159.79, 39.21, 9.03]** — best over last is **17.69×**, the largest save the champion rule has
made on this task.

Beside the two treatment probes still running, that reads like a result:

```
expEEa  [250.1, 240.2, 203.5]      ctlEEa  [172.9, 178.9]
expEEb  [269.3, 161.2]             ctlEEb  [159.8,  39.2, 9.0]
```

Three later nodes in the treatment arm all stay within 0.6–0.96 of the first; the control arm has
one that holds and two that fall to a quarter and a twentieth. It is exactly the behaviour
`--exploit-best` is written to produce.

**It does not survive the test.** The comparison has to be matched — today's runs start in the good
regime (§114), so their later nodes can only stay or fall, while a corpus run that started at 25 can
only rise. Restricted to runs whose node 0 was already ≥ 150:

| | runs | later nodes | held ≥ 0.5 of node 0 |
|---|---|---|---|
| corpus | 5 | 10 | 5 |
| today, both arms | 4 | 6 | 4 |

Exact one-sided Fisher **p = 0.451**. And within today, treatment 3/3 against control 1/3 is three
observations per arm — §83's table says a 1.25× effect needs six per arm for even 50 % power, and
this is not that.

So: nothing is claimed. The arm needs the rest of its nine. What this entry records is the shape of
the near-miss — four sequences that looked like the hypothesis, a matched comparison that says
`p = 0.45`, and a decision not to write the exciting version. §108 and §112 both had to be corrected
by a later sweep; this one gets corrected before it is written.

## 117. §109's arm at three finished probes, and the two runs the champion rule carried

**expEEa** (treatment) — TEST **251.3522**, third-best `edge_expansion` this bench has produced,
against a best train of 250.1131 (ratio 1.005, inside the band). 51-line Cython champion. $1.0182,
33 % of it before the first node and 5 % after the last. 26 `eval_train`. Reference 11.1 % import /
11.1 % `is_solution`. Money: `plan_step` 33.8 %, `propose` 28.2 %, `deep_research` 21.8 %,
`plan` 8.6 %. Nodes **[250.11, 240.17, 203.53]** — best over last 1.23×.

**ctlEEa** (control) — TEST **179.1429** against best train 178.8763 (ratio 1.001). 32-line kernel.
$1.0104, 34 % before / 0 % after, 17 `eval_train`, reference 10.7 % / 7.1 %. Nodes
**[172.93, 178.88, 0.00]** — the last node scored ZERO with `build_ext ok` and 41.8 s of evaluation,
so the extension compiled and the solver then failed validation. Not a ruler failure (§2's 0.1 s
signature is absent); a real zero, and the champion rule is the only reason this run reports 179
instead of nothing.

That is two of the three finished runs saved by the rule — `ctlEEb` at 17.69× last sweep and now
`ctlEEa` at infinity. §84 measured the rule's protective value at p = 7.45e-09 and it keeps earning
it.

### Standing, and nothing claimed

| arm | finished | TEST |
|---|---|---|
| `--exploit-best` | 1 (+1 running) | 251.35 |
| shipped card | 2 | 179.14, 156.91 |

One against two is not a comparison. Three more launched on the freed lanes — `expEEc`, `expEEd`
(treatment) and `ctlEEc` (control), card fingerprints verified distinct (`fd23da29` against
`16426855`), streaming on, no timings warning in any probe log. That takes the arm toward §83's nine.

*Also checked and not a defect:* the whole 15,492-call ledger holds **21 × 504** (all from `remEE`,
unstreamed, 51 hours ago), **2 × 503** and **1 × 400**. The three non-504s are instant rejections
with no tokens, retried by the client and invisible in the outcome — `expEEb` carried one 132
minutes ago and is still running normally. Three events in fifteen thousand calls is not a rate.

## 118. The treatment arm walks away from its winner too

**expEEb** (treatment) finished: TEST **268.2484**, the second-best `edge_expansion` this bench has
produced, against a best train of 269.3112 (ratio 0.996). 40-line Cython champion, $1.0137, 26 % of
the dollar before the first node and 0 % after the last, 17 `eval_train`, reference 14.3 % import /
14.3 % `is_solution`. Money: `propose` 27.6 %, `plan_step` 24.0 %, `repropose` 22.6 %,
`deep_research` 13.7 %.

Its nodes are **[269.31, 161.17, 27.69]**. Best over last: **9.72×**.

That is the behaviour `--exploit-best` exists to prevent, in the arm that carries the clause. §116
had four sequences that looked like the clause working; the fifth is the clause's own arm dropping
from 269 to 28 in two moves. Nothing was claimed there and there is less to claim now.

### Standing at four finished

| arm | TEST |
|---|---|
| `--exploit-best` | **268.25**, **251.35** |
| shipped card | 179.14, 156.91 |

Both treatment runs above both control runs — and with two per arm the smallest one-sided rank-sum p
attainable is 1/6 = 0.167. The ordering is what it is; it is not evidence yet. Three probes are
running (`expEEc`, `expEEd`, `ctlEEc`).

### §114 is weaker than four points made it look

`ctlEEc`'s first node came in at **20.57** — the low cluster. Today's first nodes are now
`250.1, 269.3, 250.0, 172.9, 159.8, 20.6`: **5 of 6 above 150 against 5 of 27 in the corpus**, and
the one-sided permutation p moves from 0.000127 to **0.005673**. The shift is still there and it is
not the clean sweep the first four suggested. The lesson is the arithmetic one — a fifth point can
only move a p that was computed on four — and it is the reason §114 was written as an observation
with a named confound rather than as a result.

## 119. Seven first nodes, one dichotomy, no exceptions

Read at run level across every probe launched today, the first evaluated node and what was in its
directory:

| probe | node 0 | `.pyx` | `check` calls before it | of those naming `build_ext` |
|---|---|---|---|---|
| expEEb | 269.31 | yes | 4 | 2 |
| expEEa | 250.11 | yes | 5 | 2 |
| expEEd | 250.01 | yes | 10 | 10 (3 answered `ok: false`) |
| ctlEEa | 172.93 | yes | 5 | 5 |
| expEEc | 165.30 | yes | 3 | 3 |
| ctlEEb | 159.79 | yes | 3 | 1 |
| **ctlEEc** | **20.57** | **no** | 4 | **0** |

**Six kernels between 159.8 and 269.3, one non-kernel at 20.57, and nothing in between.** The
`check` column moves with it and cannot do otherwise: with no `.pyx` there is nothing for the build
gate to report, so `ctlEEc` is the one run whose four pre-node checks never mention `build_ext`.

`expEEd` is the clearest single view of §99 working: **ten checks before its first node, all ten
carrying a build result, three of them `ok: false`.** The gate caught three broken kernels and the
model fixed them before spending an evaluation — which is precisely what the corpus could not do,
because before §99 a `check` on a Cython solver silently validated the pure-Python fallback.

This is the variable §108 measured and §114 watched move, and it appeared in no column of the sweep
summary. `probe_summary.py` now prints `node 0 kernel` / `node 0 NO kernel` on every probe's detail
line, for every task — `discrete_log`'s corpus reads 4 with and 7 without, which is the same
question asked where §110 found the answer is different.

Four falsifiers. Two mutations redden: reading the BEST node instead of the first (the running-max
question is §108's, not this one), and staying silent when there is no kernel — which would make
silence mean two things, "no kernel" and "no node at all".

*Standing, unchanged:* four probes finished, treatment 268.25 / 251.35 against control 179.14 /
156.91, and two per arm cannot produce a one-sided rank-sum p below 1/6. Three running.

## 120. A metered call with no generation behind it, and a corpus-wide claim that did not survive its own arithmetic

`check_money` exited 1 this sweep: **`UNEXPLAINED: $+0.020898`**, the first residue since §106 made
the sign meaningful. Positive means the METER holds money the spans do not.

**It is not the read race.** §106's ordering was supposed to make positive residues possible by
construction, so that was the first suspect. Measured: the span glob takes **1.9 s** and the counter
was byte-identical before and after it — `$54.106762`, 15,770 calls, both times. Nothing flowed
through the window.

**It localises to one live probe.** Per-probe meter-minus-spans:

```
expEEc   +0.020908  (5 calls)      ← the whole residue
expEE1-4 +0.057933  (38 calls)     ← the abandoned probes, already named (§112)
every other probe   +0.000002  (1 call)   ← the preflight, already named
```

**And inside it there is a call metered twice.** Two rows, 0.20 s apart:

```
ts 1788376992.936  latency 181831.4 ms  prompt 22313  completion 25966  cost 0.0103943  tok/s     142.8
ts 1788376993.141  latency     28.6 ms  prompt 22313  completion 25966  cost 0.0103943  tok/s  907902.1
```

Nine hundred thousand tokens per second. `expEEc`'s `spans.jsonl` holds **one** generation for that
call — 182.1 s, the same 22,313/25,966 usage, the same $0.0103943. One call, one span, two meter
rows, both counted: `meter.jsonl` summed over the counter's window is `$54.216969` and `/healthz`
reports `$54.216969` to the cent.

### The part I could not stand up

A detector for "impossible throughput" — `tok_per_s > 10000` — flags **254 rows (1.55 %) worth
$0.5656**, median latency 20.5 ms, and 249 of them have a same-token twin. That reads like a
corpus-wide double-metering worth half a dollar.

**It cannot be.** If $0.57 of the counter were phantom, the meter-minus-spans gap would be $0.57;
the whole gap is $0.079 and all but $0.021 of it is already named. So most of those 254 are not
duplicates — a repeated prompt with a cached or trivially short answer produces the same token
counts honestly, and my key (arm, prompt, completion) cannot tell those apart from a double-log.

What is measured is narrower and stands: **one live probe, five metered calls with no generation
span, $0.0209, at least one of them a proven duplicate.** What is not measured is how often that
happens — and no fix ships on a detector whose own arithmetic contradicts it.

`check_money` behaved correctly throughout: it named the parts it could, refused to call the rest
explained, and exited 1. That refusal is the reason any of this was looked at.

## 121. §120's residue is growing, and four hypotheses about it are dead

`check_money` now reports **`UNEXPLAINED: $+0.055313`**, up from $0.0209 an hour earlier. It sits on
two probes and nowhere else:

```
expEEd  +$0.034416  (8 extra metered calls)
expEEc  +$0.020908  (5)
expEEa, ctlEEa, ctlEEb, ctlEEc  +$0.000002 each  (1 = the preflight)
```

Four things it is not, each measured rather than argued:

* **Not the read race.** The span glob takes 1.9 s and the counter is byte-identical either side of
  it.
* **Not a double WRITE.** `proxy.py` has exactly one `fh.write` and one `Meter.record`; each of the
  four `record()` call sites is on a mutually exclusive path and returns. Two rows means two
  REQUESTS reached the proxy, not one request logged twice.
* **Not end-of-run flushing.** If the engine stopped writing spans before its last calls, the extra
  rows would sit after the final span. Rows after the last generation span: **0 in all four probes
  checked.**
* **Not corpus-wide double metering.** A `tok_per_s > 10000` detector flags 254 rows worth $0.566,
  and a "nested inside another call's window" detector flags 10,297 worth $27.76 — the second is
  simply what concurrency looks like (§100 measured >100 % wall-clock occupancy). Neither survives
  the arithmetic: the entire meter-minus-spans gap is $0.079.

What stands is one proven instance and its shape: two rows 0.20 s apart with identical
22,313/25,966 tokens and identical cost, the second at 907,902 tok/s and 28.6 ms — and **one**
generation span covering both, 182.1 s long, starting before the first row and ending after the
second. A second request, inside the window of the first, that the engine never recorded as a
generation.

That points at a retry the engine does not see — its own client re-sending while a response is in
flight — and it is money the engine's budget accounting cannot know it spent. **It is not proven,
and it is not being fixed on this evidence.** What the next sweep needs is the proxy logging a
request fingerprint so a re-send is identifiable as such; that is a one-field change and it is the
right next step, not another detector fitted to the same 254 rows.

### The two probes that finished

**expEEd** — TEST **252.5617** against best train 250.0101 (ratio 1.010). 50-line kernel, $1.0059,
33 % before the first node and 10 % after the last, 31 `eval_train`, reference 9.1 % / 13.6 %.
Nodes [250.01, 151.25, 143.54]. Money: `plan_step` 37.8 %, `propose` 30.3 %, `deep_research` 16.0 %.

**expEEc** — TEST **236.7576** against best train 233.0148 (ratio 1.016 — the new high end of
`edge_expansion`'s band, previously 1.015). 48-line kernel, $1.0087, 28 % before / 1 % after, 22
`eval_train`, reference **30.0 % import / 20.0 %** `is_solution` over 10 `run_probe` calls, the
highest reference use in the corpus. Nodes [165.30, 131.79, 233.01] — the only run today whose LAST
node is its best.

**Arm standing at six finished:** treatment **268.25, 252.56, 251.35, 236.76**; control **179.14,
156.91**. Four against two, every treatment run above every control run. Exact one-sided rank-sum
over that split is **p = 1/15 = 0.0667** — below nothing conventional, and the first time this arm
has been able to produce a p at all. `ctlEEc` is finishing and will make it 4 v 3.

## 122. The ledger now describes the request, so the next re-send is a fact instead of an inference

§121 ended by naming the one thing that would settle §120's residue, and this ships it: every meter
row carries `req_sha`, sixteen hex characters of SHA-256 over the raw request body, stamped by BOTH
row builders — the streaming path and the non-streaming one.

The reason it is the right next step rather than a fifth detector: nothing in a meter row described
the REQUEST. Two rows with the same tokens and cost can be a re-send or two honest calls, and every
detector tried in §120–§121 was really a guess about which — the throughput one claimed $0.566
against a total gap of $0.079, and the nesting one claimed $27.76 when nesting is just what
concurrency looks like (§100). With a fingerprint the question stops being statistical: same bytes
up, same `req_sha`, and a repeat is visible in one `sort | uniq -d`.

Five falsifiers. Two mutations redden: hashing an empty body (which would give every bodyless
request one shared non-empty fingerprint, reading in the ledger as "all the same request"), and
stamping only one of the two row builders — the streamed and non-streamed paths build rows
separately, so half a ledger would be unable to answer the question the field was added for.

**Not yet in force.** The running meter is 1138773, started 2026-08-31, and restarting it would zero
the counter — which would make §120's $0.055312 fall outside `--since` and vanish unexplained rather
than explained. The field takes effect at the next restart, and the residue stays on the books until
then. It stopped growing the moment the last probe finished, which is itself consistent with a
re-send while a call is in flight.

### `ctlEEc`, the last probe of the batch

TEST **197.8487** against a best train of 199.944 (ratio 0.990). 45-line kernel, $1.0069, 20 % of
the dollar before the first node and 12 % after the last, 20 `eval_train`, reference 12.5 % / 12.5 %.
Nodes **[20.57, 199.94, 24.32]** — the run that started without a kernel (§119's single exception),
found one on its second draw, and left it again on its third. Best over last: 8.22×.

**§109's arm, all seven finished:**

| arm | TEST |
|---|---|
| `--exploit-best` | 268.25, 252.56, 251.35, 236.76 |
| shipped card | 197.85, 179.14, 156.91 |

Four against three, and every treatment run still above every control run. Exact one-sided rank-sum:
**p = 1/35 = 0.0286**. That crosses §83's floor for the first time — and §83's own rule is that the
floor is necessary, not sufficient: it asked for nine per arm against a 1.25× effect, this is four
and three, and the ratio here is 1.2–1.7×. The honest statement is that the arm is now worth
finishing, not that it has finished.

## 123. The fingerprint is in force, and it answered a question in its first twenty rows

With every probe finished there was no cheaper moment, so the meter was restarted by pid (never
`pkill -f`): old counter `{"calls": 16018, "cost_usd": 54.829011, "errors": 38}` and 16,552 ledger
lines recorded first, `kill -TERM 1138773`, same argv back up as **121069**, `/healthz` answering
zeroes. §120's `$0.055312` now sits in rows older than the counter's start; it is written down here
and in §121 and it is no longer reconcilable, which is the price of getting the instrument.

**It paid inside twenty rows.** Among the first 21 fingerprinted calls:

| | |
|---|---|
| repeats within ONE arm | **0** |
| fingerprints appearing in more than one arm | 7 |

`c52cce89ef6eb93e` appears four times — once per probe, ten prompt tokens, the preflight. And
`9d82ac1ff0073c03`, `553c14b0c9e819eb`, `2be8a5fdd3c014c9` each appear **twice, in `expEEe` and
`expEEf`**, at 7,990 / 8,195 / 9,681 prompt tokens: two probes on the same card and the same task
sending byte-identical requests early in their runs.

That is the correction §120 needed and could not make. Two rows with identical token counts are the
NORMAL case across arms — same card, same task, deterministic opening prompts — so the corpus
duplicates chased there were very likely cross-arm twins rather than re-sends. The detector has to
be `(arm, req_sha)`, not `req_sha`, and now it can be.

### The arm, continued

Four more launched to take §109 toward §83's nine: `expEEe`, `expEEf` (treatment) and `ctlEEd`,
`ctlEEe` (control), fingerprints verified distinct (`fd23da29` against `16426855`), streaming on.
That will make it **6 treatment against 5 control**, from the current 4 v 3 at p = 1/35 = 0.0286.

## 124. The re-send is real, the engine makes it, and it is not a metering defect

`req_sha` settled §120 on its first working day. Eight repeats within a single arm appeared in the
new ledger, in two clearly different shapes:

**Legitimately re-asked** — same body minutes apart, *different* cost and thousands of deltas:
`expEEf c30de582 ×2, 98.85 s apart, $0.002208 then $0.002203`. Two real calls.

**The §120 shape** — four cases, all identical:

```
ts …789.24  deltas 13478  latency 118097.1 ms  932/13783 tok  $0.00398972
ts …907.39  deltas     1  latency     16.2 ms  932/13783 tok  $0.00398972
ts …907.41  deltas     1  latency     18.5 ms  932/13783 tok  $0.00398972
```

Same `req_sha`, same tokens, same cost to the cent, arriving **16 and 18 milliseconds after** the
118-second stream that produced them. One forwarded delta carrying a usage frame that restates the
whole completion.

**And the engine records all three.** `expEEf`'s `spans.jsonl` holds three generations for that
request — 118.14 s, 0.022 s, 0.064 s — each stamped `cost 0.00398972`. Meter and spans agree
exactly. So this is **not** a metering defect and never was: the ENGINE issues the same request
three times, gets a cached echo twice, and charges its own budget full price for both.

That is the correction this section exists to make. I had a `check_money` category written and
subtracting these as meter-side noise; the subtraction drove the residue to **$-0.020559**, exactly
the echo total, which is what happens when you remove from one side of a balance that already had
them on both. Reverted before commit — the arithmetic said no.

### Size, measured off the engine's own spans

The spans are authoritative and exist for every probe, so the question can be asked retroactively
without `req_sha`. A generation whose (prompt, completion, cost) repeats an earlier one in the same
run and lasts under 0.5 s:

| | |
|---|---|
| generations in the corpus | 16,800, $57.9251 |
| **echoes** | **262 = 1.56 %, $0.5554 = 0.96 %** |

About one per cent of everything this campaign has spent went on answers it had already received.
Six or seven per probe, spread evenly across every task and both arms — `remEE2` 7, `remEEref8` 7,
`accPde` 6, `remDL3` 6, and so on down.

**Not fixed here.** The right place is the engine's client, not the meter, and touching retry
behaviour while an arm is running changes the thing being measured. What this sweep establishes is
that the phenomenon is real, is the engine's, costs about 1 %, and is now identifiable in one line —
which is what the previous four sweeps could not do.

*Also:* the `$0.5554` here and the `$0.5656` a throughput detector claimed in §120 are the same
population. That detector was fitting the right rows for the wrong reason, and its own arithmetic
contradicted it because it was measured against a gap the echoes were never in.

## 125. §114 at eleven first nodes, and §124's echo rate holds on fresh traffic

Two standing numbers re-measured as the data arrived, both of them mine to be wrong about.

**§114's first-node shift, now n = 11.** Today's first evaluated nodes on `edge_expansion`, sorted:

```
269, 259, 250, 250, 213, 199, 173, 167, 165, 160, 21
```

Ten of eleven at or above 150, against **5 of 27** in the corpus (median 27.7). One-sided
permutation test on ranks: **p = 0.0000187**.

The p has moved 0.000127 (n=4) → 0.005673 (n=6) → **0.0000187** (n=11). §118 recorded the middle
value as the honest weakening a fifth point forced; the seventh through eleventh have pushed it back
the other way, which is what a real effect does and a fluke does not.

And the highest first node of the entire batch — **258.73** — belongs to `ctlEEd`, a CONTROL. That
is the third independent way this has said the same thing: the node-0 shift is not `--exploit-best`,
it is the harness (§99, §102, §103), and §115's confound — that these are different days on a shared
endpoint — is still the one thing that would explain it away and still unresolved.

**§124's echo rate, on traffic the corpus never saw.** The four probes launched after the meter
restart, measured the same way off their own spans:

| | generations | echoes | money |
|---|---|---|---|
| corpus (§124) | 16,800 | 262 = **1.56 %** | $0.5554 = 0.96 % |
| the four live probes | 657 | 11 = **1.67 %** | $0.0245 = 1.14 % |

Same rate, fresh traffic, a restarted meter and a code path that now stamps `req_sha`. The engine
re-asking a question it has already had answered is a steady one-and-a-half per cent of everything
this bench runs, not an artefact of the batch it was found in.

*Arm standing, unchanged at seven finished:* treatment 268.25 / 252.56 / 251.35 / 236.76, control
197.85 / 179.14 / 156.91, p = 1/35 = 0.0286. The four running take it to 6 v 5.

## 126. §85's cutoff instrumentation has never fired, and now there is a reason rather than a wait

Shipped on 2026-08-30 with tests and a falsifier, `cutoff_seconds` / `cutoff_spend` record what a
`plan_step` was cut BY when the wall takes it. Every sweep since has reported the same thing: it has
never recorded live data. That has been sitting as "wait for a run that cuts" for four days.

Measured across the whole corpus — **22,237 events, 0 with either field** — and then the reason,
which took one more query:

| how a finished run ends | runs |
|---|---|
| `budget_exhausted` | **48 of 48** |
| the wall | **0** |

`run_finished reason=budget_exhausted` in all 48, and `finalize_step` agreeing in all 48. There is
no wall cut anywhere in this bench's history, so the field that records one cannot appear. §85's
"twelve cut sessions, all cut by time" is a fact about a DIFFERENT corpus — repo tasks with hour-long
trainings — and does not transfer to a $1 AlgoTune probe, which runs out of money first every single
time.

So the item closes as **not applicable here** rather than as unverified. The instrumentation is
correct, tested, and dead on this bench; it stays because repo tasks are what it was written for. And
the fact underneath it is one this notebook already leans on — §101 used "50 of 50 end on
`budget_exhausted`" to explain why a third slower endpoint buys no fewer nodes. That number is now 48
of 48 on the current corpus and it has never had an exception.

*Arm, second nodes in:* `expEEe` 198.58 → **21.83** (treatment, collapsed), `expEEf` 167.21 →
**212.91** (treatment, improved), `ctlEEd` 258.73 → **235.75**, `ctlEEe` 213.06 → **260.11** (both
controls held). Two of each direction in each arm; nothing to read, and it is worth writing down that
there is nothing to read.

## 127. The arm's p moved the wrong way, which is what §83 said it would do

Three more finished, and the ranking is no longer clean:

```
268.2 T   259.8 C   252.6 T   251.4 T   236.8 T   221.6 T   198.4 T   197.8 C   179.1 C   156.9 C
```

`ctlEEe` — a CONTROL — came in at **259.7561**, second overall. Exact one-sided rank test at
6 v 4: **p = 0.08571** (18 of 210), against **0.0286** at 4 v 3 two sweeps ago. Median ratio
244.05 / 188.50 = **1.295**.

**That is the predicted behaviour, not a surprise.** §83's power table sized this bench against a
1.25× effect and said six per arm gives 50 % power; the observed ratio is 1.295 and the arm is at
six and four. A p that crosses 0.05 at n = 7 and retreats at n = 10 is what an underpowered
comparison does whichever way the truth lies. The 0.0286 in §122 was recorded with "necessary, not
sufficient" attached to it, and this is why.

Nothing is claimed. The arm continues.

### The three probes

**ctlEEe** (control) — TEST **259.7561** against best train 260.1077 (ratio 0.999). 58-line kernel,
$1.0137, 42 % of the dollar before the first node and 0 % after the last, 23 `eval_train`, reference
2.9 % / 2.9 % — the lowest reference use of the batch. Nodes **[213.06, 260.11, 26.79]**, best over
last **9.71×**.

**expEEf** (treatment) — TEST **221.5792** against 218.66 (ratio 1.013). 38-line kernel, $1.0078,
24 % before / 0 % after, 27 `eval_train`, reference 9.1 % / 4.5 %. **Four evaluated nodes**, the only
four-node run of the batch: **[167.21, 212.91, 218.66, 211.40]** — up, up, and then a hold within
3 % of the peak. Best over last **1.03×**, the tightest ending in the corpus.

**expEEe** (treatment) — TEST **198.3876** against 198.5776 (ratio 0.999). 46-line kernel, $1.0138,
35 % before / 0 % after, 29 `eval_train`, reference 9.5 % / 9.5 %. Nodes **[198.58, 21.83, 131.82]**
— a collapse to the low cluster and a partial recovery. Best over last 1.51×.

So within the treatment arm this sweep: one run that held its peak across four nodes, and one that
fell to 21.8 and climbed back. The clause is not doing one thing.

*Champion rule, again:* `ctlEEe` reports 259.76 instead of 26.79 because the best evaluated node is
what gets submitted. That is three of the batch's ten runs it has now carried.

## 128. Two controls in a row land near the top, and the arm's p is now 0.214

`ctlEEd` — control — finished at TEST **260.7523**, against a best train of 258.7298 (ratio 1.008).
46-line Cython champion, $1.0117, 31 % of the dollar before the first node and 0 % after the last,
27 `eval_train`, reference 11.8 % / 11.8 %. Nodes **[258.73, 235.75, 28.76]**, best over last
**9.00×** — the fourth run of this batch the champion rule has carried.

It is the second-highest score of the whole arm, and the control before it (`ctlEEe`, 259.76) is the
third. The ranking:

```
268.2 T   260.8 C   259.8 C   252.6 T   251.4 T   236.8 T   221.6 T   198.4 T   197.8 C   179.1 C   156.9 C
```

| n | one-sided exact rank test |
|---|---|
| 4 v 3 (§122) | **0.0286** |
| 6 v 4 (§127) | **0.0857** |
| **6 v 5** | **0.2143** |

Median ratio 244.05 / 197.85 = **1.234**, essentially unchanged from 1.295 — the arms' centres have
not moved much; what has changed is that the control's spread now reaches the treatment's top.

**This is the whole reason §83 exists, playing out in public.** Seven probes gave p = 0.0286 and it
was recorded with "necessary, not sufficient" attached. Four more probes — two of them controls that
scored 260 — took it to 0.2143. Any of the three numbers, read alone, would have supported a
different conclusion, and the only thing that separates them is n.

Nothing is claimed, and the earlier 0.0286 is now formally superseded rather than merely qualified.

*Also measured, not an anomaly:* a live pid appeared on a probe that had already written
`final.json`. It was gone on the second reading — the graded scoring pass exiting — and the probe's
log shows `[23:04:04] ИТОГ` two seconds later. A process seen once is not a process.

Three more launched (`ctlEEf`, `ctlEEg`, `expEEg`) to bring the arm to **7 v 7**.

## 129. Both low first nodes belong to the control arm, and that is not evidence

`ctlEEg`'s first node came in at **26.39** — the second first-node in the low cluster today, after
`ctlEEc`'s 20.57. Both are controls. The full split, thirteen first nodes:

```
treatment (n=7):  269  267  250  250  199  167  165      median 250.0
control   (n=6):  259  213  173  160   26   21           median 166.4
```

7 of 7 treatment at or above 150, 4 of 6 control. Exact one-sided Fisher **p = 0.1923**; rank test
on the values **p = 0.0688**.

**Neither crosses, and the question is post-hoc.** §114 stated plainly that `--exploit-best` should
NOT touch node 0 — there is nothing to make a variant of when the first idea is proposed — and used
that to argue the node-0 shift was the harness. Going back to the same data to ask whether the
clause moves node 0 after all is exactly the move that manufactures findings, and §128 has just
finished demonstrating what small n does to a p that looked good.

There is a mechanism if one wants one: the clause is in the card from the first prompt, and telling
a model that an unrelated second idea "is a fresh draw from the same distribution, not progress"
could plausibly make it invest more in the first. That is a hypothesis with a p of 0.07 behind it at
n = 13, which is a thing to keep counting, not a thing to say.

What it does change is the reading of §114: the node-0 shift can no longer be attributed to the
harness *alone* without noting that the treatment arm's first nodes sit higher than the control's on
the same day and the same box. Both statements are underpowered; both are now written down.

*Money and lanes:* residue $0.000000, three probes running, no finished runs this sweep.

## 130. Measured on what the clause actually says, the arm has a signal — on transitions, not on scores

Every reading of §109's arm so far has compared SCORES: eleven runs, one number each, a p that walked
0.0286 → 0.0857 → 0.2143 as n grew. But the clause does not promise a score. It says: *when a version
scored well, the next one should be a variant of it.* That is a claim about TRANSITIONS, and a run
with three nodes carries two of them.

Every transition in this corpus that STARTS from a node carrying a compiled kernel:

| group | transitions | next node also has a kernel | held ≥ 0.5 of the previous score |
|---|---|---|---|
| corpus (old harness, no clause) | 28 | 14 = **50 %** | 11 = 39 % |
| control (new harness, no clause) | 9 | 5 = **56 %** | 3 = 33 % |
| **treatment (`--exploit-best`)** | **12** | **11 = 92 %** | **10 = 83 %** |

**The two no-clause groups agree with each other** — 50 % against 56 %, Fisher p = 0.538 — which is
what licenses pooling them, and is itself worth stating: the harness repairs of §99/§102/§103 moved
the FIRST node (§114) and left this statistic alone. Different mechanism, different place.

Against the pooled 19 of 37:

* keeps the kernel: **11/12 against 19/37, exact one-sided Fisher p = 0.0122**
* holds half the score: **10/12 against 14/37, p = 0.0071**

§108's finding was that of 28 corpus transitions from a kernel node, 14 went back to a kernel and 14
walked away — a coin flip off a 166× median onto a 26×. The clause was written for exactly that
number, and in its own arm the coin flip is 11 of 12.

### Why this is stronger than the score comparison and still not a result

It is better powered: 12 and 37 observations instead of 7 and 6, because each run contributes every
transition it makes. It is pre-registered in the only sense available — §109 named this behaviour as
what the clause is for, before any of these probes ran, and §108 named the statistic. And it is
mechanism rather than outcome, which is where a small n has the best chance of seeing anything.

But: the pooling leans on 9 control transitions agreeing with 28 corpus ones, and the score
comparison the clause was ultimately supposed to move is at p = 0.2143 and drifting the wrong way.
A clause can change what the loop DOES without changing what it GETS — §94 measured exactly that for
the reference affordance, where use fell to zero and the score did not move.

So: the arm now has one thing worth finishing for, and it is not the number I have been reporting
each sweep. **Nothing is claimed until the ninth probe of each arm lands.**

## 131. A third of everything this campaign has spent went after the answer was already in hand

Not "after the last node" — §108 measured that at 3.6 % and it is small. This is the money spent
after the node that ENDS UP BEING THE CHAMPION has already been evaluated: the run has its answer,
and keeps going.

| | |
|---|---|
| corpus total | **$20.57 of $63.11 = 32.6 %** |
| median per run (n = 49) | **26.0 %**, range 0–67 % |
| `discrete_log` | 36.3 % | 
| `edge_expansion` | 26.0 % |
| `pde_heat1d` | 33.2 % |

Every task, every arm, a quarter to a third of the dollar. §108 established that a dollar buys two
to three draws and that the first costs 38 % of it; this says that of the remainder, most is spent
after the winner is already on disk. The champion rule (§84) is what makes that survivable rather
than fatal — it is also what makes it invisible.

### By arm, descriptively

| | median share spent after the champion |
|---|---|
| corpus | 26.0 % |
| control | 27.3 % |
| **treatment (`--exploit-best`)** | **65.6 %** |

That fits §130 exactly: the treatment keeps making kernel variants (92 % of transitions against
51 %) which hold their score (83 % against 38 %) — and it finds its champion early, so nearly
two-thirds of its dollar goes on nodes that neither beat it nor fall far from it.

### The reading I did not take

Multi-node runs where a LATER node beat the first: corpus **25/27 = 93 %**, control 5/7, treatment
**2/6 = 33 %** — Fisher one-sided p = 0.0095, which reads as "the clause stops the loop improving on
its first draw".

**Matched, it disappears.** Restricted to runs whose node 0 was ALREADY ≥ 150 — the only fair
comparison, because a run that starts at 25 has nowhere to go but up and the treatment's node 0 is a
kernel 7 times out of 7 (§129):

| | later node beat the first |
|---|---|
| corpus | 4/5 |
| control | 2/4 |
| treatment | 2/6 |

Fisher **p = 0.2308**. The 93 % was the corpus climbing out of the low cluster, not the corpus
improving on a good start. Third time this month a striking split has dissolved under matching
(§116, §129, this) — and third time it was checked before it was written rather than after.

What stands unconditioned is the headline: **a third of the campaign's money is spent after the
answer is already found**, and no arm of this experiment has changed that.

## 132. The arm reaches seven against seven: scores p = 0.0487, transitions p = 0.0060

All three finished. `expEEg` (treatment) took TEST **264.9858** against a best train of 267.4695
(ratio 0.991) — 42-line kernel, $1.0060, 26 % of the dollar before the first node and 5 % after the
last, 28 `eval_train`, reference 4.2 % / 4.2 %, nodes [267.47, 217.59]. `ctlEEf` (control) took
**218.8688** against 216.7143 (1.010) — 20-line kernel, $1.0120, 43 % before / 0 % after, 32
`eval_train`, nodes [143.51, 216.71, **7.90**], best over last **27.42×**, the largest save the
champion rule has made in the whole corpus. `ctlEEg` (control) took **172.0602** against 169.5875
(1.015) — 27-line kernel, $1.0083, 29 % / 6 %, 32 `eval_train`, nodes [26.39, 169.59, 31.97], and it
is the second run of the batch whose node 0 carried no kernel.

**Both arms at seven.**

```
268.2T 265.0T 260.8C 259.8C 252.6T 251.4T 236.8T 221.6T 218.9C 198.4T 197.8C 179.1C 172.1C 156.9C
```

| statistic | value |
|---|---|
| scores, exact one-sided rank test, 7 v 7 | **p = 0.0487** (167/3432) |
| median ratio | 251.35 / 197.85 = **1.270** |
| transitions from a kernel node that keep the kernel | T **12/13** against no-clause **20/40**, **p = 0.0060** |
| …that hold ≥ 0.5 of the previous score | T **11/13** against **15/40**, **p = 0.0035** |

The two statistics have behaved very differently as n grew:

| n | scores | transitions (keep) |
|---|---|---|
| 4 v 3 | 0.0286 | — |
| 6 v 4 | 0.0857 | — |
| 6 v 5 | 0.2143 | — |
| 11 runs | — | 0.0122 |
| **7 v 7** | **0.0487** | **0.0060** |

**The score p has now been on both sides of 0.05 three times.** It is not a number to act on and it
was never going to be at this n — §83 asked for nine per arm against a 1.25× effect and the observed
ratio is 1.270, which is exactly the case that table was built for. The transition statistic has
moved one way only, on 53 observations rather than 14, and it is measuring what the clause literally
says.

What can be said after fourteen probes and about $14: **the clause changes the loop's behaviour, and
whether that behaviour is worth anything to the score is still not established.** §94 is the standing
precedent for exactly this shape — an affordance whose use went to zero with the score untouched.

Two more probes per arm would reach §83's nine. That is the first time in this programme that
finishing a measurement is a smaller decision than starting one.

## 133. Written before the last four land: what this arm will be allowed to conclude

`expEEh`, `expEEi` (treatment) and `ctlEEh`, `ctlEEi` (control) are running, fingerprints verified
(`fd23da29` against `16426855`). When they finish the arm is **nine against nine**, which is what
§83 asked for and where it stops.

This section is written NOW, with those four still running and their scores unknown, because the
score p has already been 0.0286, 0.0857, 0.2143 and 0.0487 on the same growing sample. A statistic
that has crossed 0.05 in both directions three times is one I could report either way once the data
lands, and the only defence against that is to say beforehand which reading counts.

**Primary, decided in §109 before any probe ran:** the clause claims that when a version scores
well, the next should be a variant of it. That is the transition statistic — from a node carrying a
compiled kernel, does the next node carry one, and does it hold at least half the score. At 7 v 7
it is 12/13 against 20/40 (p = 0.0060) and 11/13 against 15/40 (p = 0.0035).

**Secondary:** the TEST score, exact one-sided rank test. At 7 v 7, p = 0.0487, median ratio 1.270.

**What each outcome will be allowed to say at 9 v 9:**

| transitions | scores | conclusion |
|---|---|---|
| holds | holds | the clause changes behaviour AND the behaviour pays; ship it on by default |
| holds | does not | the clause changes behaviour and the score does not follow — §94's shape; **do not ship**, and record that a behavioural win is not a score |
| does not hold | either | the 7 v 7 transition result was n, not signal; the clause goes back in the drawer |

**And the confound that survives either way:** §115. Every probe of both arms ran on days when the
shared endpoint was measurably different from the corpus's (§101: a third of its throughput), and
§114/§129 could not separate "our harness repairs moved the first node" from "the endpoint moved".
That does not touch the arm — its control is concurrent — but it does mean **nothing here transfers
to the corpus's numbers**, and any comparison of these fourteen (soon eighteen) runs against the
older forty-nine has to say so.

No result is claimed in this section. It exists so that the next one cannot quietly pick its
statistic.

## 134. The third batch's first nodes came in at 28, 23 and 9 — same harness, same cards

Same code, same two card fingerprints (`fd23da29` / `16426855`, verified at launch), same box, same
lanes. Three batches of `edge_expansion` probes, their first evaluated nodes:

| batch | first nodes | median | endpoint tok/s (median) |
|---|---|---|---|
| 1 | 269, 250, 250, 173, 165, 160, 21 | **172.9** | 107.2 (n = 2,226) |
| 2 | 267, 259, 213, 199, 167, 144, 26 | **198.6** | 111.9 (n = 2,209) |
| **3 (running)** | **28, 23, 9** | **23.1** | 97.0 (n = 393) |

`expEEi`'s 8.91 is the lowest first node this bench has ever recorded, and it is a TREATMENT probe.

**Throughput barely moved.** 97.0 against 107–112 is about 10 % — not the kind of shift §101
measured between the corpus and today (a third). So whatever is different, it does not show up in
tokens per second, which is the only endpoint property this bench can see.

### What this does to §114

§114 said the harness repairs moved the first node: 10 of 11 above 150 against 5 of 27 in the
corpus, p = 0.0000187, and §115 named the unresolvable confound — different days, shared endpoint.
Batch 3 is the natural experiment that confound asked for, and it is running now. **Same harness,
first nodes at 23.**

Under batches 1–2's rate (10 of 14 at or above 150), three consecutive low ones have probability
(4/14)³ ≈ **0.023**. Under the CORPUS's rate (5 of 27), the same three have probability ≈ **0.54** —
i.e. entirely ordinary. Three points cannot separate those, but the direction is clear enough to
state a prediction rather than wait for one:

**If batch 3's remaining first nodes stay in the low cluster, §114 is dead and §115's confound was
the answer — the endpoint moved, not our repairs.** If they come in high, batch 3 was a run of bad
luck at 2.3 %.

*Resolved one sweep later — see §135.* The fourth came in at **155.36**, above the line. Batch 3 is
1 of 4, not 0 of 4, and neither branch of the prediction fires cleanly.

Written before the fourth probe of the batch has an evaluated node, for the same reason §133 was.

*Everything else clean:* four probes live, residue $0.000000, zombies 0, seven baselines, no
`PermissionError`.

## 135. The fourth first node came in at 155, and the prediction lands between its two branches

§134 wrote the prediction before the fourth probe of batch 3 had a node. It arrived: `expEEh`,
**155.3557**, above the 150 line. Batch 3 is **155, 28, 23, 9** — one of four, not zero of four.

| | first nodes | ≥ 150 | median |
|---|---|---|---|
| batches 1–2 | 14 | **11** | 185.8 |
| batch 3 | 4 | **1** | — |
| corpus | 27 | 5 | 27.7 |

Batch 3 against batches 1–2: exact one-sided Fisher **p = 0.0833**. Not a break, not a continuation.

And §114 over all eighteen of today's probes against the corpus: **12 of 18 above 150 against 5 of
27**, Fisher **p = 0.0015**, Monte-Carlo permutation on the values **p = 0.00066** (200,000 draws;
the exact enumeration is C(45,18) and does not finish, which is its own small lesson about reaching
for the same tool at every n).

So the effect §114 measured survives, weaker than the 0.0000187 it showed at n = 11 and now resting
on a sample whose most recent quarter behaved like the corpus. **Neither branch of §134's prediction
fires**, and that is the honest outcome: the natural experiment that was supposed to separate "our
repairs" from "the endpoint" gave four points, one of them high, and separated nothing.

What it did do is bound the claim. Whatever moved the first node is **not stable across four hours
on the same harness** — batches 1–2 ran at 11 of 14 and batch 3 at 1 of 4 with identical code and
card fingerprints. A repair does not come and go. Something else in the loop's environment does, and
§115's confound is now not merely unresolved but demonstrated to be live.

*The arm itself is untouched by this:* its control is concurrent within every batch, so a batch
effect hits both arms equally. `expEEi`'s recovery is the illustration — 8.91, then **138.31**.

## 136. The durability chain, checked against live data rather than against its own tests

§98 verified the snapshot machinery by driving it — vanished destination, two concurrent writers,
`.env` named. What it did not do is ask whether the archive actually HOLDS the corpus that has been
produced since. Asked now, of every run on disk:

| | |
|---|---|
| live runs | **68** |
| archived at full length | **64** |
| archived SHORTER than live | **4** |
| missing from the archive | **0** |
| `.superseded-*` files | 0 |

The four short ones are `expEEh`, `ctlEEh`, `expEEi`, `ctlEEi` — **exactly the four probes running
right now**, whose `events.jsonl` grows between snapshots. That is the distinction §98's repair loop
was built to make and it is making it: a growing file is not a truncated one, and the archive is
short of it only until the next cycle. Zero `.superseded-*` is consistent with no task having been
re-run, which matches the attempt ledger.

The newest snapshot, `20260903-032011`, restores:

* `looplab.bundle` 30.6 MB — cloned to a bare repo, **3,702 commits**, `looplab-HEAD.txt` reads
  `f893ef9a`, which is the working tree's HEAD to the character
* `AlgoTune.bundle` 84.2 MB, `AlgoTune-dirty.txt` 150 kB — the ruler and its local modifications
* `.complete` present, written last, past the shortfall check
* `ENVIRONMENT.txt` 1,961 bytes — the redacted settings, naming the `.env` it deliberately does not copy

So the loss of 2026-08-29 — 37 unpushed commits and 69 probe runs gone with `/var/tmp` — is covered
on all three axes now: git history in a bundle that clones, the ruler beside it, and the run trees in
a sibling archive that this sweep verified holds 64 of 64 finished runs whole.

*Nothing else moved:* four probes live, residue $0.000000, zombies 0, seven baselines, no
`PermissionError`. `expEEi` continues its climb off the corpus's lowest-ever first node — 8.91, then
138.31, now **229.14**.

## 137. The arm closes at nine against nine, and §133's rule picks the middle row

All four finished. The arm is complete at the size §83 asked for, and §133 wrote down what each
outcome would be allowed to say before these scores existed.

### The four

**expEEi** (treatment) — TEST **228.8890** against best train 229.1356 (ratio 0.999). 68-line kernel,
$1.0150, 27 % before the first node and 0 % after the last, 23 `eval_train`, reference 0.0 % / 0.0 %.
Four nodes, **[8.91, 138.31, 229.14, 150.64]** — from the lowest first node this bench has recorded
to its own best, in three moves.

**ctlEEi** (control) — TEST **228.0554** against 228.9432 (0.996). 45-line kernel, $1.0099, 29 % /
0 %, 31 `eval_train`, reference 0.0 % / 0.0 %. Nodes **[28.25, 228.94, 0.00]** — the last scored
zero at 45.6 s of evaluation, a real zero, and the champion rule is the whole reason this reports
228 instead of nothing.

**expEEh** (treatment) — TEST **158.6335** against 156.8733 (1.011). 48-line kernel, $1.0039, 48 %
before / 9 % after, 27 `eval_train`, reference 12.5 % / 12.5 %. Nodes **[155.36, 156.87]** — two
nodes, the second a 1 % improvement on the first.

**ctlEEh** (control) — TEST **26.1659** against 26.6076 (0.983). 84-line kernel, $1.0096, 30 % / 3 %,
27 `eval_train`, reference 12.0 % / 12.0 %. Nodes **[23.06, 26.61, 9.01]** — the lowest score in the
entire arm, and a run that never left the low cluster.

### The two statistics, at 9 v 9

```
268T 265T 261C 260C 253T 251T 237T 229T 228C 222T 219C 198T 198C 179C 172C 159T 157C 26C
```

| | value |
|---|---|
| **secondary — TEST score**, exact one-sided rank test | **p = 0.0567**, median ratio 1.197 |
| **primary — keeps the kernel** after a kernel node | T **15/16** against **20/41**, **p = 0.00130** |
| **primary — holds ≥ 0.5 of the score** | T **14/16** against **15/41**, **p = 0.00055** |
| primary, against the CONCURRENT control only | T 15/16 against 6/13, **p = 0.00670** |

The score p across the whole arm: 0.0286 → 0.0857 → 0.2143 → 0.0487 → **0.0567**. It never settled
and it does not cross.

### §133's rule, applied

> transitions hold, scores do not → the clause changes behaviour and the score does not follow —
> §94's shape; **do not ship**, and record that a behavioural win is not a score.

**So `--exploit-best` stays OFF by default.** The clause does what it says: after a node with a
compiled kernel, the treatment arm proposes another kernel 94 % of the time against 49 % without it,
and keeps at least half the score 88 % against 37 %. Both hold against the concurrent control alone,
so this is not a batch effect. And the score it was meant to buy is at p = 0.0567 after eighteen
probes and about $18 — a median ratio of 1.197, which is below the 1.25× §83 sized the bench for.

**This is the second time this programme has measured a real behavioural change worth no score.**
§94's reference arm moved use from 8.4 % to 0.0 % with the score untouched at p = 0.4811. The pattern
is now a finding in its own right: *this loop's behaviour is much easier to move than this loop's
result.*

What the arm cost: eighteen probes, about $18, and it answered its question. What it did not do is
tell us how to make the loop better — and it is worth saying plainly that a correctly executed,
pre-registered, adequately powered experiment ending in "do not ship" is the outcome this programme
has been unable to produce until now.

## 138. §115's arm is live, and the manipulation is visible in the runs rather than only in the card

A card that names a different script proves nothing about what the loop experienced. Measured in the
probes' own spans, `check` calls so far:

| probe | `check` calls | answers naming `build_ext` | `ok: false` |
|---|---|---|---|
| oldCK1 (pre-§99 checker) | 4 | **0** | 0 |
| oldCK2 (pre-§99 checker) | 7 | **0** | 0 |
| newCK1 (shipped checker) | 5 | 0 | 1 |
| newCK2 (shipped checker) | 5 | **2** | 1 |

Eleven checks on the old checker, not one of them mentioning a build — which is what
`looplab_check_pre99.py` does, because it has no `build_gate`. Two on the new one already carry a
build result. §119 established that column as the mechanism's signature; it is now the arm's
manipulation check, and it passes.

`card_sha256` is `164268558e1a0469` for all four — identical cards apart from the `check` command's
argv, verified in §137's tests and again here on the live trees.

**First nodes so far: `oldCK1` 25.41, `oldCK2` 160.95.** One in each cluster, and the new-checker
pair has not evaluated yet. Nothing to read, which is the expected state two hours in.

The prediction from last sweep stands, unchanged and written before any of these four scores:
**if the `oldCK*` first nodes come in systematically below `newCK*`, §114 was about our repairs; if
they do not differ, §114 was about the endpoint and §115's confound closes in the confound's
favour.**

*Everything else clean:* four probes live, residue $0.002172 (inside tolerance, the in-flight and
echo noise §124 characterised), zombies 0, seven baselines, no `PermissionError`.

## 139. Both of §99's defects reproduce live, in the arm built to test them

§99 fixed two things in `check`: a false GREEN (a guarded Cython import falls through to pure Python,
so the checker certifies a path the grader will not run) and a false RED (`ModuleNotFoundError` on a
multi-file solver, because the submission's directory was not on `sys.path`). Both were measured off
the corpus's history and pinned by tests. Neither had ever been watched happening.

Now they have. The four probes of §115's arm, with their node files and their `check` answers:

| probe | checker | node files | `check` calls | naming `build_ext` | `ModuleNotFoundError` |
|---|---|---|---|---|---|
| oldCK1 | pre-§99 | `solver.py` | 4 | 0 | 0 |
| **oldCK2** | **pre-§99** | **`edge_solver.pyx`, `setup.py`, `solver.py`** | **10** | **0** | **1** |
| newCK1 | shipped | `solver.py` | 9 | 0 | 0 |
| **newCK2** | **shipped** | **`solver_kernel.pyx`, `setup.py`, `solver.py`** | **5** | **2** | **0** |

`oldCK2` and `newCK2` wrote the same SHAPE of solver — a Cython kernel, a `setup.py`, and a
`solver.py` that imports it — on the same task, from cards identical but for the checker's path. The
old checker answered with a `ModuleNotFoundError` and never once mentioned a build; the new one
carried two build results and no import failure. **Both defects, side by side, in the same hour.**

`oldCK1` and `newCK1` both wrote a single-file pure-Python solver, where neither mechanism can bite,
and neither shows anything. That is the control within the control, and it is what makes the pair
above readable rather than anecdotal.

### The arm itself is still uninformative

First nodes: **old {25.41, 160.95}, new {20.76, 275.22}** — one in each cluster, in each arm. At two
per side this is precisely the state §83's table describes, and the only honest thing to report is
that there is nothing to report.

What has been established is narrower and worth having: the manipulation is real, it is visible in
the runs, and it reproduces both of the specific failures the repair was written for. Whether that
repair moved the first node is what the arm is still measuring.

*Clean:* four probes live, residue $0.000000, zombies 0, seven baselines, no `PermissionError`,
snapshot `20260903-052452` complete.

## 140. The first probe of §115's arm ran the NEW checker and never wrote a kernel at all

`newCK1` finished: TEST **25.3697** against a best train of 24.7567 (ratio 1.025). $1.0035, 40 % of
the dollar before the first node and 8 % after the last, 26 `eval_train`, reference 9.5 % / 9.5 %
over 42 `run_probe` calls. Money: `plan_step` **46.7 %** — the highest planning share of the batch —
`propose` 18.5 %, `deep_research` 18.0 %, `repropose` 6.4 %.

Its champion is **34 lines of plain Python**, and its three nodes are **[20.76, 21.82, 24.76]**: a
run that never left the low cluster, on the repaired checker.

| node | files | |
|---|---|---|
| node_0 | `solver.py` | |
| node_1 | `solver.py` | |
| node_2 | `solver.py` | |

Twelve `check` calls, none naming `build_ext` — **because there was never a `.pyx` to build.** The
repaired checker cannot help a run that does not attempt a kernel, which is exactly the shape §119
found in `ctlEEc` and §137 in `ctlEEh`.

**This is the first direct evidence against §114's mechanism.** The claim was that repairing `check`
lets the loop commit to a compiled kernel on its first draw; here is a probe with the repaired
`check` that wrote pure Python three times and scored 25. The repair is necessary for a kernel to be
validated, and this run says it is not sufficient to make one be attempted.

*State of the arm at one finished probe of four:*

| | first nodes |
|---|---|
| pre-§99 checker | 25.41, 160.95 |
| shipped checker | **20.76**, 275.22 |

Still one in each cluster on each side. `oldCK2` has meanwhile climbed 160.95 → **221.64** and
`newCK2` 275.22 → 254.43. Nothing to conclude, and §133's discipline applies here too: the reading
was fixed before the data and it needs all four.

## 141. §115's arm closes without an answer, and the reason is that I built it too small

All four finished. Same task, same box, same hour, cards identical but for the `check` command's
argv.

| probe | checker | TEST | node 0 kernel | nodes |
|---|---|---|---|---|
| newCK2 | shipped | **268.5174** | yes | [275.22, 254.43, 8.43] |
| oldCK2 | pre-§99 | **223.2247** | yes | [160.95, 221.64, 166.07] |
| oldCK1 | pre-§99 | **145.2586** | no | [25.41, 150.20, 7.01] |
| newCK1 | shipped | **25.3697** | no | [20.76, 21.82, 24.76] |

**One of two on each side wrote a kernel at node 0.** Fisher p = 0.8333. TEST by arm: one-sided rank
test **p = 0.6667**, and the smallest value obtainable at 2 v 2 is 0.1667 — the arm could not have
produced a significant answer no matter how the scores fell.

### That is my mistake, and it is the same one this notebook keeps recording

§83 built a power table. §133 pre-registered a stopping rule. §137 closed a nine-against-nine arm on
exactly that discipline. Then I launched a **two-against-two** arm to answer a question about a
binary outcome whose corpus rate is 5/27 = 19 % and whose recent rate is 12/18 = 67 %. Simulating
Fisher against those two rates:

| n per arm | power |
|---|---|
| 6 | 0.36 |
| 10 | 0.59 |
| **12** | **0.74** |
| 18 | 0.89 |

**Twelve per side**, about $24, is what this question costs. I spent $4 on a design that had roughly
a one-in-eight chance of showing anything, having written the rule against exactly that four sweeps
earlier. Four probes' worth of lanes were free, so I filled them — which is how an experiment gets
sized by the hardware rather than by the question.

### What the four probes do say, descriptively

Node-0 kernel rate today: **2 of 4 = 50 %**, sitting between the corpus's 19 % and the earlier
batches' 67 %, and consistent with the batch drift §134/§135 measured on identical harnesses. The
checker made no visible difference to it at this n, and §140 already showed the sharper version: a
probe with the REPAIRED checker that wrote plain Python three times and scored 25.

**§115's confound stays open.** It is now open with a price attached — 24 probes, about $24 — which
is more than it was worth before this arm and exactly as much as it was worth after §133.

*The three not yet analysed:* `oldCK2` TEST 223.2247 against best train 221.6356 (ratio 1.007),
53-line kernel, $1.0145, 30 % before / 6 % after, 37 `eval_train`, reference 0.0 % / 5.9 %.
`oldCK1` 145.2586 against 150.2013 (0.967), 63-line kernel, $1.0081, 25 % / 8 %, 26 `eval_train`,
reference 7.4 % / 7.4 %, nodes ending on 7.01 — best over last 21.44×. `newCK2` 268.5174 against
275.2249 (0.976), 40-line kernel, $1.0099, 33 % / 0 %, 22 `eval_train`, reference 10.7 % / 10.7 %,
last node 8.43 — best over last **32.64×**, the largest champion-rule save in the corpus.

## 142. §115's arm, sized by the question this time: twelve per side, in batches, stopping rule first

§141 measured what the question costs — Fisher power against a corpus rate of 19 % and a recent rate
of 67 % reaches 0.74 at **twelve per arm** — and named the failure that produced a two-against-two
design: four lanes were free, so four probes ran. This section commits to the size before the first
probe of the rerun, so that the same thing cannot happen again by increment.

**Design, fixed now:**

* **24 probes total**, 12 on `looplab_check_pre99.py` and 12 on the shipped checker, `edge_expansion`,
  $1 each, ≈ $24. Cards identical but for the `check` command's argv (`card_sha256`
  `164268558e1a0469` on both sides, verified in §137's tests and on the live trees in §138).
* **Run four at a time**, two per arm, one arm per lane pair, so every batch is internally paired and
  a batch effect — which §134/§135 measured as real — hits both arms equally.
* **The four probes of §141 count.** They were run under this exact design and nothing about them was
  selected after the fact: `oldCK1` 145.26, `oldCK2` 223.22, `newCK1` 25.37, `newCK2` 268.52. That
  makes the rerun 10 more per arm, ≈ $20.
* **Primary statistic:** does node 0 carry a compiled kernel. Corpus 5/27 = 19 %, recent batches
  12/18 = 67 %, §141's four 2/4. Exact one-sided Fisher, new against old.
* **Secondary:** the TEST score, exact one-sided rank test.
* **Stop at 12 v 12 regardless of what the numbers do in between**, and report whatever is there.

**What each outcome will be allowed to say:**

| node-0 kernel rate | conclusion |
|---|---|
| new clearly above old | §114 was our repairs; the endpoint explanation dies |
| indistinguishable | §114 was the endpoint (or something else that moves between batches); §99/§103 remain correct repairs that did not move the first draw |
| old above new | the repair costs something; §99 gets re-examined against its own tests |

The third row is written because §140 already contains a run of its shape — the repaired checker,
three plain-Python nodes, TEST 25.37.

**Batch 1 of 6 launches now.** No result is claimed in this section; it exists so the next five
cannot be sized by whatever hardware happens to be idle.

## 143. The pre-registered statistic now lives in the summary, and it makes §129 visible

§142 fixed "does node 0 carry a compiled kernel" as the primary reading of §115's arm. §141 is the
record of what hand-rolling that analysis per sweep costs. §119 put the fact on each probe's line;
`probe_summary.py` now prints the RATE on each card's row, so the comparison a pre-registered arm
reads is computed in one place and cannot be computed a different way next sweep.

It immediately shows something no sweep had put side by side — every `edge_expansion` arm, one
column:

| card | node 0 carried a kernel |
|---|---|
| `--exploit-best` | **8/9 = 89 %** |
| shipped card, today | 6/11 = 55 % |
| `pre-99 checker` | 1/2 = 50 % |
| `--no-reference-affordance` | 3/9 = 33 % |
| `--no-unteachable-rules` | 2/8 = 25 % |

Exact one-sided Fisher:

* `--exploit-best` against today's shipped card: **p = 0.1192**
* today's shipped card against the two older control arms (5/17): **p = 0.1753**
* `--exploit-best` against the two older arms: **p = 0.00558**

**This is §129's question with more data and it still does not resolve.** §129 asked whether the
clause moves node 0 — which §114 said it should not — and found p = 0.0688 on 13 first nodes. Now
`--exploit-best` sits at 89 % against a shipped card that is itself at 55 %, and the gap between them
is p = 0.12 while the gap to the OLD arms is p = 0.006. Two candidate stories fit: the clause moves
node 0, or the harness/endpoint moved everything after §99 and the older arms are simply older. The
third column above — 1/2 on the pre-99 checker, running right now — is the arm that separates them,
and it is 4 of 24 probes in.

The tally is deliberately blind to arms with no evaluated node: counting a run that has not evaluated
yet as "no kernel" would make every arm look worse the earlier it is read, which is the same
censoring the spend row already names. Three falsifiers; the mutation that counts those runs reddens
one.

*Batch 2 of 6 is 30 minutes in with no nodes yet;* residue $0.000000, zombies 0, seven baselines, no
`PermissionError`.

### §143.1 — the mutation guard in §143 guarded nothing, and I pushed it that way

The test named `test_a_probe_with_no_node_is_not_counted_as_a_no` was written to redden if the tally
counted runs that have not evaluated yet. Run against that mutation, it stayed **green**.

The fixture built its no-node probe with an EMPTY `events.jsonl`, and `summarise()` returns `None`
for one — so the probe never entered the group, the mutated line never saw it, and the assertion
`2/2 = 100%` held for the wrong reason. A run that has STARTED and not yet evaluated is the real
case, and it needs at least one event; with a `run_started` row in the fixture the mutation reddens
as intended.

Committed and pushed before the mutation was checked, which is the process failure underneath the
test failure — this notebook's own rule is that a hole is only closed once the mutation reddens, and
§143's commit message asserted it had. Both are corrected here rather than quietly.

## 144. The batch effect is as large as the thing being measured, so §142's statistic is the wrong one

Batch 2 of §115's arm has three first nodes so far — `oldCK3` 27.66, `newCK3` 22.79, `oldCK4` 23.34
— **all in the low cluster, in both arms.** That is the third batch to behave as a batch rather than
as two arms, so it is time to measure the effect instead of noting it again.

Node-0 kernel rate by LAUNCH HOUR, every `edge_expansion` probe that recorded an instrument:

```
09-01 04  0/1     09-01 18  0/3     09-02 15  4/4 100%
09-01 06  1/1     09-01 19  0/1     09-02 18  2/3  67%
09-01 07  1/1     09-01 20  2/3     09-02 21  4/4 100%
09-01 10  0/1     09-01 21  1/2     09-02 23  2/3  67%
09-01 12  0/2                       09-03 02  1/4  25%
09-01 13  1/2                       09-03 04  2/4  50%
09-01 16  1/2                       09-03 07  0/3   0%
```

Overall 22/46 = 48 %. Across the nine batches with n ≥ 3 the rates are
**0.00, 0.67, 1.00, 0.67, 1.00, 0.67, 0.25, 0.50, 0.00**.

| | |
|---|---|
| observed variance of batch rates | **0.1424** |
| variance expected if the rate were constant at 0.48 | **0.0724** |

**Twice the binomial spread.** There is a real between-batch component, and it is about the size of
the effect §142 set out to detect (19 % against 67 %).

### The amendment

§142 pre-registered *exact one-sided Fisher, new against old, pooled*. That statistic is wrong for
this design and I should have seen it when I wrote the design that makes it wrong: the probes are
**paired within batch** — two per arm, launched together, on the same box — precisely so a batch
effect hits both sides equally. Pooling throws that pairing away and lets the between-batch variance
back into the denominator.

**Amended, before any of the outstanding twenty probes finish:** the primary reading becomes the
**batch-stratified exact test** — each batch contributes its own 2×2, combined conditionally
(Mantel–Haenszel / exact conditional). The pooled Fisher will still be reported beside it, and if the
two disagree the stratified one is the one that counts, because the stratification is a fact about how
the probes were run and not a choice made after seeing them.

`n` stays at 12 per arm. Under pooling, 2× overdispersion would have cost roughly half the effective
sample and left the design at the power of six a side — which is where §141's post-mortem said this
programme keeps ending up. Stratifying recovers it, which is why the amendment is worth making rather
than buying twelve more probes.

*Also true and worth stating plainly:* the same measurement retires the corpus-versus-today
comparison in §114 for good. Between-batch variance of that size cannot be averaged away by adding
probes to one side of a comparison whose sides ran on different days. Only the within-batch arm can
answer it.

## 145. The stratified statistic is now a file with tests, and it corrected §144's own reading

§144 amended the arm's primary reading to a batch-stratified exact test. §143 is the record of what
hand-rolling a pre-registered statistic per sweep costs. So it is `benchmarks/stratified_arm.py`:
each batch contributes a 2×2, the null distribution of the summed cell is the exact convolution of
the per-batch hypergeometrics, and the pooled Fisher is printed beside it for contrast.

Six falsifiers, and they pin the properties that matter rather than the arithmetic:

* a single stratum reproduces Fisher exactly (2/2 against 0/2 gives 1/6);
* the same imbalance seen in two batches gives (1/6)² — stratifying rewards repetition;
* **a batch where both arms did the same thing cannot move the p** — the mutation guard, because an
  implementation that let ties contribute would shrink a p by adding uninformative probes;
* pooling and stratifying disagree on a Simpson's-shape input, which is the whole reason §144
  amended the statistic instead of buying more probes.

Replacing the stratification with a pooled sum reddens three.

### And it caught me on §144's own sentence

§144 said batch 2's first nodes were "all in the low cluster, in both arms", and I wrote it in a
section about the KERNEL rate — implying none carried a kernel. The tool says otherwise, and the
files agree with the tool:

| probe | node 0 | files |
|---|---|---|
| oldCK3 | 27.66 | `solver.py` |
| newCK3 | 22.79 | `solver.py` |
| oldCK4 | 23.34 | `solver.py` |
| **newCK4** | **22.77** | **`setup.py`, `solver.py`, `solver_kernel.pyx`** |

**A compiled kernel that scored 22.77.** Low score and no kernel are not the same fact, and §108's
whole finding — kernel median 166 against ~26 — is a median, not a law. One of the four wrote the
kernel and still landed in the low cluster.

*The arm so far, by the amended statistic:* two batches have both arms, arm A (shipped checker) 2/4
against arm B (pre-§99) 1/4, **stratified p = 0.50000**, pooled 0.50000. Four of twenty-four probes
in, and the two readings agree because neither has anything to disagree about yet.

## 146. The first node is the noisiest thing in the run, and the run recovers from it

Batch 2's second nodes came in: `oldCK3` 27.66 → **230.44**, `newCK3` 22.79 → **221.81**,
`oldCK4` 23.34 → **245.43**. A batch whose first nodes were all in the low cluster climbed out of it
on the very next draw, in both arms.

That is worth measuring rather than noting, because it decides whether node 0 is a sensible outcome
at all. Across the nine `edge_expansion` batches with three or more probes, the median of each
quantity per batch:

| quantity | per-batch medians | spread | **CV** |
|---|---|---|---|
| node 0 score | 23.7, 52.6, 211.5, 165.3, 205.8, 143.5, 25.7, 93.2, 23.1 | 188.5 | **0.747** |
| best train node | 202.7, 219.4, 214.5, 233.0, 238.7, 216.7, 192.9, 185.9, 226.1 | 52.8 | **0.083** |
| final TEST | 205.6, 220.4, 215.3, 236.8, 240.7, 218.9, 193.3, 184.2 | 56.4 | **0.091** |

**The batch moves node 0 by eight times as much as it moves the answer.** Whatever differs between
batches — the shared endpoint, the hour, something invisible from here — it lands almost entirely on
the first draw, and the run walks it back. Every batch's final score sits between 184 and 241.

### Second amendment to §142, and this one is about the outcome, not the test

§144 amended the statistic (pooled → stratified) because the batch variance was as large as the
effect. This is worse than that: **the outcome itself was the wrong choice.** Node 0 is where the
noise lives; it is not where the result lives.

So the primary reading of §115's arm becomes **the final TEST score**, exact one-sided rank test,
still stratified by batch. Node-0 kernel rate stays as a secondary, descriptive, and clearly labelled
as measuring an intermediate the run recovers from.

That reframes three earlier sections rather than retracting them. §114 (first nodes moved after the
repairs, p = 0.0000187), §129 (both low first nodes were controls) and §143 (`--exploit-best` at 8/9
against 6/11) are all statements about node 0 — which is now measured to be the least stable and
least consequential thing the run produces. They are still true; they are just about a quantity that
does not reach the score.

**And the honest consequence for the arm:** the final score's own batch spread is 184–241, a factor
of 1.31. §83's table sizes this bench against 1.25×, needing nine per arm. The arm is at twelve per
arm by §142, which is adequate — but only for an effect at least that large, and nothing so far
suggests the checker produces one.

*Clean:* four probes live, residue $0.002977 (in tolerance), zombies 0, seven baselines, no
`PermissionError`.

## 147. Three runs of the same batch traced the same arc, and the champion rule caught all three

`oldCK3`, `newCK3` and `oldCK4` finished within the hour, on two different checkers, and their node
sequences are the same shape to within noise:

| probe | checker | nodes (train) | TEST | best over last |
|---|---|---|---|---|
| oldCK4 | pre-§99 | **[23.34, 245.43, 24.71]** | **244.3203** | 9.93× |
| newCK3 | shipped | **[22.79, 221.81, 27.81]** | **225.8641** | 7.98× |
| oldCK3 | pre-§99 | **[27.66, 230.44, 23.72]** | **221.7132** | 9.71× |

Low, high, low. Three times, in both arms, in one batch. All three champions are Cython kernels
(40, 53 and 34 lines) found on node 1 and abandoned on node 2, and **all three runs report a number
only because the best evaluated node is what gets submitted** — without §84's rule they would report
24.7, 27.8 and 23.7.

Ratios to train: 244.3203/245.4259 = 0.995, 225.8641/221.8094 = 1.018, 221.7132/230.4424 = 0.962 —
the last one is `edge_expansion`'s new low, just under §111's previous 0.963. Money: `plan_step`
36–44 %, `propose` 17–26 %, `deep_research` 17–22 %, `repropose` 5–12 %. `eval_train` 24, 34, 24.
Reference use 0.0 %, 4.8 %, 6.7 % — the first is a run that never touched the reference at all.

**This is §108's running-max shape at its cleanest, and it is what makes node 0 a bad outcome
(§146).** Every one of these runs would be read as a failure at node 0 and as a success at node 1.
The batch that looked ruined two sweeps ago finished with three scores between 221 and 244, i.e.
inside the corpus's normal band.

*Arm state, amended primary (final TEST, stratified by batch):* batch 09-03T04 gave shipped
{268.52, 25.37} against pre-§99 {223.22, 145.26}; batch 09-03T07 has shipped {225.86, …} against
pre-§99 {221.71, 244.32} with one probe still running. Eight of twenty-four probes in; no test is
computed here because a batch with an incomplete arm is not a stratum yet.

## 148. The money cue reaches 99 % of the spend, and I measured 32 % — the same truncation the tool beside me was written to prevent

Two things were chased this sweep. Both looked like leaks and both shrank on measurement; the
second one shrank because the FIRST measurement was wrong, in the exact way this repo already had a
library to prevent.

### 148.1 The novelty gate is not a leak — 87 % of its rejections land the node ten minutes later

`newCK4` spent **25.9 % of its budget and 50.7 minutes after its last evaluated node** — against
0.3 %, 1.2 % and 3.7 % for the three probes of its own batch (§147). The event stream says why: a
`propose` that ran 2367 s, a `novelty` stage that ran 1003 s, and then `novelty_rejected` for
node 2, `near_node 1`.

Corpus-wide that shape is common: **59 of 76 runs (78 %) hold at least one rejection, 77 in all**,
and the window ending in one costs a median $0.2305 — 22.9 % of a $1 run, $16.43 in total, of which
$12.23 (74 %) is `propose` + `repropose` + `novelty`.

That paragraph is the leak I was about to report, and it is wrong. What happens after a rejection:

| outcome | n | share |
|---|---|---|
| the SAME node id is evaluated later (median **9.9 min**) | 67 | **87 %** |
| no node evaluated after it | 10 | 13 % |

The rejection is a redirect, not a discard: the loop reproposes and the node lands. Of the ten
terminal cases, seven had **less than $0.01 left** — the rejection is simply the last event before
the run stops. Only three had real money on the table: accPde $0.2347, remEE $0.2034,
newCK4 $0.1273. **Corpus cost of the whole phenomenon: $0.6569.**

*Corrected an hour later, by the run itself.* `newCK4` does not belong in that list. It was still
running when I measured it, and it went on to evaluate node 2 at 22.101 — its rejection was a
redirect like the other 87 %, and it read as terminal only because the file ended where the clock
was, not where the run did. The true figures are **9 runs and $0.5296**, and the lesson is the
narrow one: a live run cannot be scored on "what happened after", because nothing has. The runs that end this way have a
median of **1** evaluated node against 3 for the rest, so a terminal rejection marks a starved run
rather than causing one.

### 148.2 Item 8(в): closed, and the number that said otherwise was mine

The standing list says the money hint does not reach `plan` / `foresight_rank` / `hyp_prioritize`.
Grepping the spans for `Spend guidance` agrees loudly — 0 of 1347 `plan_step` spans, 0 of 305
`plan`, 0 of 741 `deep_research`. Widening the pattern to all three wordings the loop actually
uses (`Spend guidance`, and the two `BUDGET: $` lines in `repo_developer.py` and
`deep_research.py`) moved it to 31.7 % / 21.3 % / 83.3 % and put the blind share of spend at
**65.6 %**.

Both figures are artefacts. `core/tracing.py` stores `input` as the SUFFIX whenever `input_from` is
set, so a chained prompt reads as blind however well it is served. Resolving the chain with
`benchmarks/algotune/span_input.py` — a file written on 2026-08-28 after this identical mistake, whose
own docstring says a naive reader "reported the budget line in 3 of 32 step prompts when the true
figure is 35 of 35" — gives the real answer over the 12 newest runs (3,922 generations, $12.1123):

| phase | spans | sees the money | share of spend |
|---|---|---|---|
| plan_step | 1354 | **99.3 %** | 36.6 % |
| propose | 797 | 90.5 % | 24.5 % |
| deep_research | 742 | 83.3 % | 18.2 % |
| repropose | 268 | 92.5 % | 8.3 % |
| plan | 305 | **100 %** | 8.1 % |
| foresight_rank | 96 | 0 % | 1.6 % |
| hyp_prioritize | 61 | 0 % | 0.8 % |
| novelty | 146 | 0 % | 0.7 % |

**Blind spend is $0.9862 of $12.1123 = 8.1 %, not 65.6 %.** `plan` was closed on 08-31 by the
Developer's own `_budget_note()` and the closure holds at 100 %. What is still blind is
`foresight_rank` + `hyp_prioritize` (2.4 %), left blind on purpose — a ranker choosing among
candidates it did not generate has no cheaper option to switch to.

Items (а) and (б) are also shipped, checked against the built card word for word, not by paraphrase:
"AND THERE IS A CEILING ON HOW SLOW YOUR SOLVER MAY BE, PER INSTANCE. The harness gives each
instance's subprocess `(1 + 5) * reference_time * 10` seconds"; and "THE BEST EVALUATED SOLVER IS
WHAT GETS SUBMITTED, NOT YOUR LAST ONE."

### 148.3 The fix is an instrument, because the library was not enough

`span_input.py` existed, was correct, and did not stop me repeating the mistake it was written for —
a library only helps the reader who remembers to import it. So the resolution is now a command:
`benchmarks/cue_reach.py <probe-dir>… [--pattern REGEX] [--naive]`, which walks a probe tree, scores
a cue against the RESOLVED prompt, and with `--naive` prints the truncated-read figure in a column
beside it so the gap is on the page:

```
phase                   spans   sees      %      cost  share   naive%
plan_step                 249    249 100.0% $  0.8136  40.2%    33.7%
propose                   135    123  91.1% $  0.4890  24.2%    10.4%
```

`tests/test_the_cue_reach_tool_reads_the_whole_prompt.py` holds it: a two-span fixture where the cue
is stated in the parent and carried by the child, so a resolving reader scores 2 of 2 and a
truncated one scores 1 of 2. Mutated three ways — drop the resolve, narrow the default pattern to
one wording, `glob` instead of `rglob` — and each mutation reddens a different assertion.

The default pattern being a union of three wordings is the second trap, independent of the first:
one regex per repo would have reported the Developer and the Researcher blind. The test pins that
too, by asserting the narrow pattern is visibly narrower.

## 149. The ruler still reads 0.98, and the way I nearly mis-read it

`edge_expansion`, reference wrapped in a `Solver` and scored against itself on the TEST split,
regime `__w22x1r3`: **speedup 0.9796, eval_seconds 43.0**, against 0.9847 on the standing card. The
ruler holds.

Getting there took four wrong readings, all mine, and the first two are the exact signature point 2
of the sweep list warns about — a zero with a tiny `eval_seconds`:

| attempt | result | what was actually wrong |
|---|---|---|
| `/opt/conda` python, no env | 0.0, `eval_seconds` **1.7** | `DATA_DIR` unset; regime came out `__lane22r3`, not `__w22x1r3` |
| `/opt/conda`, campaign env | 0.0, 1.7 | `solver_unloadable` — the reference file defines a Task, not a `Solver` |
| shim, wrong import path | 0.0, 1.7 | `AlgoTuneTasks.edge_expansion` is a package; the module is one level down |
| **AlgoTune's own `.venv`** | **0.9796, 43.0** | — |

The pinned `eval_train` command in every card names `/var/tmp/looplab-bench/AlgoTune/.venv/bin/python`.
Under `/opt/conda` the evaluator loses `huggingface_hub`, skips dataset generation and reports a
clean `speedup: 0.0` — a full ruler refusal that looks exactly like a solver scoring nothing.
`eval_seconds` is the tell and it is the only tell: 1.7 s against 43.0 s.

## 150. The audit agent's thirty findings: first five re-derived by hand

The agent finished with 30 numbered findings. Re-deriving each with my own command, not its script;
what has survived so far:

| # | claim | agent | mine | verdict |
|---|---|---|---|---|
| 26 | the card's `eval_train` timings are stale | n=791, 42.1 / 3.6 / 117.3 s | **identical** | CONFIRMED, fixed in `846e8f74` |
| 13 | probes fetch published solutions to their own graded task | 52 of 76 (68 %), all 52 with solver source | **52 of 76, 52 with source** | CONFIRMED |
| 5 | more `run_probe`, worse score | r = −0.36 (n=53) | r = **−0.41** (n=53), median split 228.94 vs 177.60 | CONFIRMED in direction; my probe count is inflated (I matched `run_probe` anywhere in the span, so my split point 47 is not its 23) |
| 15a | the probe cannot import `reference_<task>.py` because its first import is `AlgoTuneTasks` | 11 failures name it | the file's line 11 is `from AlgoTuneTasks.base import register_task, Task`, and my own ruler shim hit it | CONFIRMED |
| 20 | the money cue now reaches every deciding role and changed nothing | plan_step 96.2 %, plan 86.0 % | **99.3 % / 100 %** (§148, different run subset) | CONFIRMED |

Finding 13 is the one that matters beyond its dollar figure. **68 % of the corpus read published
AlgoTune solver source for the task it was being scored on**, and the card's fence — "the evaluator
and the timer are fenced and are not yours to look at" — is prose, while `web_fetch` is a hole. The
measured effect on score is NEGATIVE in my data too (fetched median 209.71, n=46; not fetched
238.40, n=7), so this is a validity hazard rather than score inflation, and n=7 makes even that a
weak statement. No result in this document has been stratified on it.

The remaining 25 findings are unvalidated. The largest by size is #1 — 84.7 % of prompt tokens are a
byte-identical re-send priced at a flat tier, $47.39 of $75.87 — and it is the next one to check,
because if the gateway does cache and the proxy's imputation is blind to it, every dollar figure in
this document is wrong in the other direction.

## 151. newCK4 closes batch 2 four-for-four on the same arc, at a fifth of the height

| | |
|---|---|
| TEST | **106.3578** |
| nodes (train) | **[22.769, 106.686, 22.101]** |
| train→test ratio | 106.3578 / 106.686 = **0.997** |
| best over last | 4.83× |
| champion | 50-line Cython kernel, node 1 |
| spend | $1.0080 — plan_step 36.6 %, propose 27.1 %, plan 12.5 % |
| `eval_train` | 21 calls |
| reference use | **15.0 % import / 12.5 % `is_solution`** over 40 `run_probe` calls |
| node 0 | carried a kernel |

Low, high, low — a fourth time, in the same batch, and again the champion rule is the only reason
this is a 106 and not a 22. But it is the batch's runt: 106.36 against 221.71, 225.86 and 244.32,
on the same task, in the same batch, on the same checker as newCK3. It is also the batch's slowest
(183 min against 121-147) and the only one whose node 0 already carried a kernel — it started where
the others ended up and then found less.

Two numbers stand out. Its reference use, 15.0 % import and 12.5 % `is_solution`, is **two to three
times §69.1's 4.9-8.3 % band** — the highest in the batch by a wide margin (the others: 0.0 %,
4.8 %, 6.7 %). And 1 of its 3 evaluated nodes is graded-unchecked. Whether leaning on the reference
module is what cost it the other 120 points is not answerable from one probe, but it is the first
time the two have moved together this visibly, and it is worth a column in the arm's table.

Batch 2 final: shipped checker {225.86, 106.36}, pre-§99 {221.71, 244.32}. Eight of twenty-four
probes; still no test, because a two-per-arm batch is a stratum with a variance and not an answer.

## 152. Audit finding #1, measured: there is no cache discount to recover, and the gateway is already serving cached bodies at full price

The agent's largest finding says 84.7 % of prompt tokens are a byte-identical re-send priced at a
flat tier — $47.39 of $75.87 — and offers a remedy: "have the proxy read and record the provider's
cache-hit token field". It also names the branch that would matter more: if the provider already
caches and the proxy's imputation is blind to it, every dollar figure in this document is wrong.

Both halves are now measured, and the remedy is the part that does not survive.

**The price is exactly flat.** Least-squares over all 22,757 priced ledger rows, my own fit:
**$0.1400/Mtok in, $0.2800/Mtok out, max |residual| 1.4e-17.** One tier, no second one hiding in
the data. This reproduces the agent's fit to four decimals.

**There is no cache-hit field to read.** Two identical requests through the meter, 4,012 prompt
tokens each, and the `usage` object came back with exactly:

```
{"completion_tokens": 2, "prompt_tokens": 4012, "total_tokens": 4014,
 "cost": 0.0005622400000000001, "cost_basis": "imputed", "cost_source": "2026-08-20T10:16:47Z"}
```

`prompt_tokens`, `completion_tokens`, `total_tokens` — the other three keys are the proxy's own
additions. No `cached_tokens`, no `prompt_tokens_details`, no `cache_read_input_tokens`. The
proxy is not blind to a discount; none is reported. **So the campaign's dollar figures stand, and
finding #1's remedy is unavailable at this endpoint.**

**But the second call took 17.7 ms against the first call's 236.4 ms** — same `req_sha`, same 4,012
prompt tokens, same $0.00056224. Something upstream served it without generating it, and billed it
whole. That is not a hypothesis: it is two rows in the ledger, and it is the mechanism behind the
agent's finding #12.

Corpus-wide, over the 6,240 rows that carry a `req_sha` (§122; **16,552 earlier rows have none and
are counted out loud rather than dropped**):

| | |
|---|---|
| bodies sent more than once | **224 (3.6 %), $0.3932** |
| median latency of the repeat | **25.1 ms** |
| median latency of the original | **4,570.1 ms** |
| repeats too fast to have been generated (<¼ the original) | **145, $0.2409** |

A 182× latency collapse at an unchanged price. `benchmarks/resent_bodies.py` reports it;
`tests/test_the_ledger_names_the_bodies_it_paid_for_twice.py` pins that only the SECOND send counts,
that an equally-slow repeat is a real second generation and not a cache hit, and that the
unstamped rows are named. Mutated three ways — count the first send, drop the skipped tally, treat
any faster repeat as cached — and each reddens a different assertion.

Nothing in the engine is changed for this: $0.24 is real and the arm is mid-flight.

### 152.1 The measurement broke the reconciler, which is how the reconciler got fixed

The two service calls above entered the ledger as an arm with no probe tree. `check_money.py` then
reported, on an otherwise clean sweep:

```
1 call(s) STILL UNNAMED -- neither killed nor empty
2 call(s) from 1 ABANDONED probe(s) ... svcCacheCheck $0.0011
RESIDUE $-0.000002
```

The same arm in two categories, and a residue that had been exactly zero all week. The cause is one
line: `probes` was built from the union of span arms and meter arms, so an ABANDONED arm — whose
entire cost is subtracted further down — was first decomposed into a preflight call plus some
unexplained extras. The dollar error is one preflight estimate per abandoned arm ($0.00000196); the
attention error is a red line on a clean ledger, and §112 is the record of what that costs.

Fixed by computing `abandoned` first and excluding it from `probes`. Residue is $+0.000000 again
and the UNNAMED line is gone. The falsifier builds an abandoned arm with TWO calls, so the old code
would read it as one preflight plus one extra; mutating the exclusion away reddens it.

## 153. The planning phase is promised a writer it does not have, in 95 % of runs, and every attempt fails

Audit finding #14, re-derived with my own census over all 76 probe trees:

| phase | `write_file` calls | errored |
|---|---|---|
| `plan_step` | 716 | 0 |
| `card_build` | 15 | 0 |
| **`plan`** | **51** | **51** |

And **504 of the 528 `plan` chain-roots (95.5 %)** carry a system prompt that names `write_file`.
The agent reported 51 refusals and 495 of 519; the difference is corpus drift over the four probes
that finished between its census and mine. The contradiction is between two adjacent strings:

* system — "You improve an existing experiment repository by WRITING code with the write_file and
  edit_file tools (edit_file for changes to existing files, write_file for new ones)."
* user, immediately below it — "This is the PLANNING stage. You can READ and inspect the repo …
  but you CANNOT write code yet."

The system prompt wins about one run in ten, and the run pays a turn to find out.

`read_only_intro()` in `adapters/repo_developer.py` is the repair: it swaps that one sentence for
the truth and returns foreign text unchanged, so an operator's own intro is never half-rewritten.

**It is not wired in.** The call site is one line inside `_propose_plan`, and it is deliberately
left unmade: §115's arm is eight probes into twenty-four and this changes what every probe is told.
`tests/test_the_plan_phase_is_not_promised_a_writer.py` pins the function's behaviour (mutated
three ways — blunt token swap, replace-everywhere, discard-the-rest — each reddens) and carries a
fourth test that FAILS the day the call site is made, so the held-back repair cannot be quietly
forgotten and cannot be quietly shipped either.

Also re-derived this sweep, without a conclusion attached: **1,891 calls to fifteen tools whose
store is structurally empty** on a single-run, no-dataset benchmark (`list_sibling_runs` 397,
`cross_run_search` 325, `data_schema` 315, `list_all_runs` 252, `data_profile` 174, `read_asset`
155 …). The agent's count is 1,869 — the same population. It also claims only one of them ever
returns a body; my emptiness test is a length heuristic and disagrees on three tools, so I am
recording the call counts, which we agree on, and not the emptiness claim, which I have not
measured properly.

## 154. The four snapshot items that were "not checked by me" — driven, and all four refuted

Three of them the standing list marks unverified, one it marks open. All four are closed in the
script already; the point of this section is that I drove them rather than read the comment above
them, because a comment claiming a repair is the thing this notebook has most often found wrong.

**(1) "a snapshot whose destination has vanished reports success."** It does not. Two gates fire
before anything is written:

```
FATAL: …/.persistent-store-id is missing but … is not empty.
       The persistent volume is probably NOT mounted. … Refusing.        exit 1
```
```
FATAL: cannot create …/snapshots/.snapshot.lock -- the destination is not writable.   exit 1
```

Driven twice — destination under a regular file, and a store whose directory is mode 555 with the
marker present. Exit 1 both times, nothing written.

**(2) "nothing separates two simultaneous snapshots."** `flock` does. Two runs launched into one
destination in the same command: **exit 0 and exit 0, two directories, 53 files each** —
`20260903-110546` and `20260903-110610`, 24 s apart because the second waited for the lock. Then the
harder case, which that run did not reach: five directories pre-created with the next five stamps,
each holding a `PREEXISTING` marker. The snapshot took **`20260903-110714-2`** and every
pre-existing directory still holds exactly its one marker file. No merge, no clobber. (`N` starts
at 2 on purpose — the second snapshot of a second is `-2`.)

**(3) "`.env` does not reach the snapshot and is not named."** Half right, and the wrong half.
`find` over the newest snapshot returns **0** `.env` files — and `ENVIRONMENT.txt` inside it names
the file it left behind, with its size and age:

```
## /home/jovyan/data/looplab/.env  (89 lines, mtime 2026-08-06T02:44:45)
```

**(4) The item marked OPEN — `campaign.sh` `rm -rf`s the task directory, then `cp -ru` overwrites
attempt 1's evidence with attempt 2's shorter log.** The list says this "closes only by versioning
the archive by attempt", and that is exactly what shipped: a `.superseded-N` loop that copies aside
anything the source is not a continuation of, BEFORE any copy runs. Both named tests pass here:
`test_a_restart_does_not_take_the_previous_attempt_with_it` and
`test_every_restart_gets_its_own_layer`.

One honest qualification, which is the only new thing in this section. The live archive holds **80
`events.jsonl` and exactly four `.superseded-1` files, and all four are `memora_cache.json`** (211–
236 bytes). The mechanism has never fired on a run log in production — no retry has needed it since
it landed. It is verified by its tests and by four firings on a JSON cache, and not yet by the case
it was built for.

## 155. A correction to the last sweep, and the anomaly that was my own clock

I reported batch 3 as "50 minutes in". It was 18. This sweep I then measured it against batch 2 at
"85 minutes" and found batch 3 spending **four times less** with zero evaluated nodes — a clean,
alarming, entirely false result: the probes started at 10:42 and it was 11:04.

The true comparison, once the elapsed time came from `INSTRUMENT.txt` and the ledger instead of
from my own memory of when I launched them: batch 3 is 22 minutes in, $0.12–$0.16 spent, calls
running at 1.98–2.76 per minute against batch 2's 1.54–2.76, median latency **3,220 ms against
batch 2's 5,197 ms**, and no errors. The endpoint is if anything faster. Batch 2's first evaluated
node arrived at 31, 32, 46 and 62 minutes, so zero nodes at 22 minutes is on schedule.

Two instruments lied in the same five minutes and both were mine: a `now=$(date +%s)` captured at
the top of a block and used after several minutes of work inside it (negative file ages), and a
`/proc` scan whose "swarm of a hundred short-lived processes" was my own shell's children,
disappearing between the listing and the read. The first one at least announced itself by printing
a negative number. The second did not, and would have become a section.

## 156. The budget gate the audit recommended would have destroyed the best node in the corpus

Audit finding #9 re-derived on my own numbers: **$5.91 of $76.73 (7.7 %) lands after the last node a
run ever evaluates**, and the price of one completed node cycle is median **$0.3370**, p75
**$0.4481**, p90 **$0.5432** over n=195 — the agent's medians to four decimals. (Its 6.9 % becomes
my 7.7 % only because the four live probes have no last node yet; excluding them and the abandoned
`remDL` gives 6.5 %. §148.1's mistake, avoided this time by excluding them up front.)

Its proposed repair is the interesting part: *"refuse to open a new node when `limit - spent` <
p75(node cycle) ≈ $0.45, and finalize instead."* That is the right shape of fix — §152 showed the
prompt-side cue reaches 99 % of the deciding generations and prevents nothing — and **the threshold
is wrong by a factor of four and a half.** Counterfactual over the 76-run corpus, every cycle
replayed against every threshold:

| gate | empty cycles cut | $ redirected | REAL nodes lost | best node lost |
|---|---|---|---|---|
| $0.05 | 49 | 0.5816 | **0** | — |
| **$0.10** | **61** | **1.5354** | **1** | **0.00** |
| $0.15 | 67 | 2.2990 | 3 | 211.40 |
| $0.25 | 70 | 2.9389 | 23 | 277.23 |
| **$0.4481 (p75)** | 74 | 4.4743 | **54** | **277.23** |

At p75 the gate buys $4.47 by refusing fifty-four cycles that produced a real node — among them the
**277.23 that is the best `edge_expansion` node in the corpus**. At $0.10 it buys $1.54 and the only
real node it costs scored **0**. The knee is between $0.10 and $0.15, and it is sharp: three nodes
and a 211.40 appear in one five-cent step.

`benchmarks/budget_gate_curve.py` computes the whole curve, because the threshold has to be
re-derived as the corpus grows and a number in a comment goes stale in silence.
`tests/test_the_budget_gate_curve_counts_what_it_would_break.py` pins that the tool reports the
node it would have killed — a version scoring only the savings recommends p75 — and four mutations
redden it (stop reporting the killed node, never count a real loss, count zero-spend trailing
cycles, and the boundary).

**The boundary mutation is worth its own line.** `remaining >= gate` mutated to `remaining > gate`
survived every test I had written; the assertion I added to catch it then failed against CORRECT
code, because `1.00 - 0.92` is `0.07999999999999996` and sits below a $0.08 gate by 4e-17. So the
real code now compares with a 1e-9 tolerance and the fixture uses binary-exact amounts. A cent of
float error at this boundary is one 277.23.

Not wired into the engine, for the same reason as §153: this changes what every probe does, and
§115's arm is eight probes into twenty-four.

## 157. Every node failure this campaign ever recorded was invisible, and the eleven of them qualify §156

Audit finding #27b, re-derived: `events.jsonl` writes a crash-atomic packet whose `type` is
`__looplab_event_batch_v1__` with the real events under `data.events`. Over the 80 run logs:
**29,571 physical rows, 11 packets, in 11 different runs, each holding exactly one `node_failed`
and one `pause`.** So the count of node failures visible to a reader that keys on `type` is
**0 of 11** — including the `fails=[]` this sweep has printed for weeks, and six of the eight tools
under `benchmarks/` that read the file. `algotune/plot_corpus_v2.py::iter_events` was the only one
that handled it.

`benchmarks/events_read.py` is the shared reader; `probe_summary.py` now uses it and reports a
`node_failed` list per run. Both packet spellings are handled — the corpus writes the sentinel as a
one-element LIST, the engine's own tests use the bare string — and a row that merely names the
sentinel without carrying events is kept whole rather than swallowed.
`tests/test_a_failed_node_is_not_invisible.py` pins all of it; four mutations redden it (list
spelling only, swallow the named-but-empty row, never expand, and the summary reverting to the
naive loader).

What the eleven say, now that they can be read:

| cause | n |
|---|---|
| `developer error: LLM spend ceiling reached` | **9** |
| `developer stuck: the implement session ended having written nothing at all` | 2 |

**And they qualify §156's answer rather than confirming it.** For each doomed cycle, what the run
held when that node opened:

| run | node | $ left when it opened | $ burnt | cause |
|---|---|---|---|---|
| accPde | 1 | **0.4792** | 0.4867 | ceiling |
| accEE | 2 | **0.4165** | 0.4208 | ceiling |
| remPde6 | 1 | 0.3818 | 0.3858 | ceiling |
| remEE | 2 | 0.3612 | 0.3627 | ceiling |
| remDL6 | 1 | 0.3560 | 0.3662 | ceiling |
| remDL3 | 1 | 0.2288 | 0.2410 | wrote nothing |
| remEE2 | 2 | 0.1112 | 0.1143 | ceiling |
| expEEh | 2 | 0.0917 | 0.0955 | ceiling |
| newCK1 | 3 | 0.0804 | 0.0839 | ceiling |
| remDL12 | 2 | 0.0717 | 0.0968 | wrote nothing |
| expEEg | 2 | 0.0486 | 0.0547 | ceiling |

Total burnt in these eleven: **$2.7084**. The $0.10 gate §156 arrived at catches **four** of them,
worth $0.3309. The p75 gate catches all eleven — and destroys 54 real nodes doing it, including the
corpus's 277.23.

So the named failures sit ABOVE the safe threshold, and a gate on remaining budget alone cannot
separate them: `accPde` opened its doomed node holding $0.4792, more than the median completed
cycle costs, and still produced nothing. The distinguishing feature is not how much was left, it is
that the session then wrote nothing and ran past the ceiling. That is a MID-session check, not an
opening gate — the developer session already knows, turn by turn, both what it has spent and
whether it has written a file, and neither fact currently ends it.

That is the shape of the next repair, and it is not this sweep's: §115's arm is eight probes into
twenty-four.

## 158. The suite was red for a file doing exactly what it was told, and `| tail` hid it

The full suite I started at the end of the last sweep reported **exit code 0 and a FAILED line in
the same output**. Both are true: the command was `pytest … | tail -5`, so the status belonged to
`tail`. That is the trap the sweep list names in so many words, walked into while checking a fix
for a different blindness.

The failure is real and mine:

```
test_open_item_index.py::test_each_slug_is_declared_exactly_once
  a slug names exactly one item; these are declared more than once:
  {'solver-check-requires-a-literal-class-statement':
     ['benchmarks/algotune/looplab_check.py (OPEN)',
      'benchmarks/algotune/looplab_check_pre99.py (OPEN)']}
```

`looplab_check_pre99.py` is `looplab_check.py` as of `103c4b1e^`, extracted verbatim so §115's arm
can run the old gate beside the new one (§134). It therefore carries its ancestor's `OPEN[…]`
marker, and the index counts the slug twice. The file cannot be edited to opt out — being
byte-identical to what that commit shipped is its entire purpose — so the opt-out has to come from
outside it.

`benchmarks/algotune/FROZEN_COPIES.txt` is that outside, one line per verbatim extract:

```
benchmarks/algotune/looplab_check_pre99.py <- benchmarks/algotune/looplab_check.py  # the checker as of 103c4b1e^, for §115's arm (§134)
```

A list of files an index is told to skip is an obvious place to make an inconvenient open item
disappear, so `test_the_frozen_manifest_cannot_hide_a_live_open_item` holds it to three rules: the
live counterpart must exist, the frozen file must still DIFFER from it (an entry that no longer
does is stale, and stale is how a live file gets excused), and **every slug the frozen file declares
must still be declared by a file the index does read** — the manifest may excuse a duplicate, never
the last copy of an open item.

Mutated four ways. Excusing `meter/proxy.py`, whose eight open items nothing else declares, reddens
it; an entry pointing at itself reddens it; removing the skip from `_iter_markers` reddens the
original duplicate test. The first attempt at the "hides a live item" mutation used
`probe_summary.py` and **passed** — that file declares no markers at all, so the check had nothing
to walk. A mutation that exercises nothing proves nothing; the second one names a file with eight.

## 159. The check fix moved false-GREEN from 5.3 % to 1.1 %, and that is p = 0.14

Two more audit findings re-derived, and the second one changes its own label.

**#30, zeros among evaluated nodes.** My census: **9 of 199 (4.5 %)**, against the agent's 9 of 194
(4.6 %) — the same nine, my denominator larger by the nodes added since. By task:
`edge_expansion` 7/161 (4.3 %), `pde_heat1d` 2/17 (11.8 %), `discrete_log` 0/21.

Worth stating against point 2 of the sweep list, which is where a zero has to be triaged: **every
one of the nine carries `eval_seconds` between 7.6 and 60.7 s.** None is the ~0.1 s signature of a
ruler refusal. These are nine solvers that ran and scored nothing, not nine evaluations that never
happened. (The 7.6 s outlier is `remEE6` node 3, the Cython build failure.)

**#18, "the check fix took."** The measurement holds. Splitting every evaluated node by whether its
probe's `looplab` commit contains `103c4b1e`, and asking whether the last `check` before the
evaluation said ok while the node then scored 0:

| | nodes | false GREEN | false RED |
|---|---|---|---|
| before the fix | 76 | **4 (5.3 %)** | 1 |
| after the fix | 90 | **1 (1.1 %)** | 1 |

The agent reported 4/73 and 1/83; same nodes, same direction, my denominators larger.

**But the label "VERIFIED" does not survive its own arithmetic.** One-sided Fisher on that table is
**p = 0.1355**. At these rates the split needs about **210 post-fix nodes** to reach p < 0.05 —
another ~120 nodes, which at three nodes a probe is roughly forty probes and forty dollars. The
direction is right, the effect is not established, and the honest line is "consistent with a real
improvement, not yet distinguishable from noise."

This matters for §115's arm and it does not change it: the arm's primary outcome is the final TEST
score stratified by batch (§146), not the false-GREEN rate, and it was never powered for the
latter. What this adds is the number to quote when someone asks whether the checker repair is
proven — it is not, and 24 probes will not prove it either.

*And a note on how nearly this went the other way.* The first Fisher I computed returned
**p = 0.98** and I was one keystroke from writing "no effect, possibly harmful": I had summed the
hypergeometric tail from 0 to the observed count instead of from the observed count up — the
probability that the OLD checker did better, which is the answer to a question nobody asked. The
tell was that a 5× rate difference cannot sit at p = 0.98 in either direction. A one-sided test has
two sides, and the wrong one is silent about being wrong.

## 160. The suite is green, and the line that says so has been suppressed by my own flag for weeks

`PYTEST EXIT=0`, `[100%]`, **0 `FAILED` lines, 0 `ERROR` lines** in a run measured without a pipe.
The suite is green, including the four new files this sweep added.

But the log has no `N passed` line at the end, and chasing that turned up something about every
suite reading in this notebook. `pyproject.toml` carries `addopts = "-q"`. Every sweep has then
typed `-q` as well — and **two `-q` flags suppress pytest's final summary line entirely**:

```
pytest tests/test_suite_health.py -q      →  -- Docs: https://docs.pytest.org/...    (last line)
pytest tests/test_suite_health.py         →  3 passed, 1 warning in 1.59s
```

Same exit code, same tests, one line of evidence present or absent. Nothing was ever hidden that
mattered — `FAILED` lines print at any verbosity, which is how §158's duplicate slug was caught —
but the COUNTS were gone, and "13,327 passed" is a claim I have been making from a summary line
that a differently-invoked run produced. The instrument that says "the suite is green" was one
redundant flag away from saying nothing at all, and it said nothing quietly.

The fix is to stop passing `-q`: the config already supplies it, and the second one is what costs
the summary. The `Fatal Python error: Segmentation fault` blocks at the top of the log are not a
problem either — they are the child process of
`test_a_solver_that_kills_its_process_is_one_failed_row_not_an_empty_report`, which exists to prove
that a solver killing itself becomes one failed row rather than an empty report.

Three instrument failures in three sweeps, all mine and all the same shape: `cmd | tail` returning
tail's status (§158), a Fisher tail summed from the wrong end (§159), and now a doubled `-q`. None
of them broke anything. Each of them removed a number I was about to quote.
