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
