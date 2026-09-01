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
`proposal_cues.py`'s a CLAIM pin with the slug `llm-budget-cue-reaches-propose-only`. The cost of the gap now has a
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
apart, and returned 183.6 and 101.2. That is a controlled pair, and it puts a floor of about 1.8×
under the noise on a single configuration.

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
floor of about 1.8× under the noise on a single configuration from the `remEE`/`remEE2` pair; this
pair says the floor is wherever a run happens to land, because one of the two never produced a valid
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
