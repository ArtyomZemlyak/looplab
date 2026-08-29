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
