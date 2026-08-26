# 53. What the AlgoTune campaign says about OUR loop (2026-08-26)

Companion to [doc 51](51-algotune-arm-operational-notes-2026-08-20.md) (what was touched in the
third-party checkout) and [doc 52](52-bench-box-jhub-l40s-2026-08-20.md) (the box). Those two are
about the *harness*. This one is about **LoopLab itself** — the eight task-arms the campaign scored
against AlgoTuner, read as evidence about our loop rather than as a scoreboard.

**Every number below was re-derived from the artefacts for this document.** Four subagents produced
an initial reading; three of their claims about LoopLab did not survive re-measurement and are
recorded here as corrections, because a defect list that overstates is worse than a short one. The
sources are `runs-B/<task>/run/events.jsonl` (the event log), `run/spans.jsonl` (the full trace,
4.7 MB per task — the tool calls are HERE, not in the event log), `run/nodes/*/score.log`, and
`champion_solver.py`.

The corpus is the eight task-arms with a scored counterpart: `convex_hull`,
`count_riemann_zeta_zeros`, `discrete_log`, `edge_expansion`, `integer_factorization`, `kcenters`,
`pagerank`, `spectral_clustering`. One model (`deepseek-v4-flash`), $1.00 each, 22-core lanes, one
shared reference. Total measured spend on those eight: **$8.029**.

---

## 0. Three claims that did NOT survive re-measurement

Recorded first, so nothing downstream rests on them.

* **"LoopLab never profiles / constant factors are invisible to the loop."** False. Of **492
  `run_probe` calls** across the eight task-arms, **197 (40.0 %) contain a timing harness**
  (`perf_counter`, `timeit`, `process_time`). The loop times things constantly. The real defect is
  narrower and is item 1 below.
* **"`read_logs` was called 0 times on `spectral_clustering`."** False as stated: `read_logs` is
  invoked **88 times** across the corpus and **13 times** on that task. Whether the *verdict* text
  reached a prompt is a separate question and is item 4.
* **"Every node carries `trials: []`, so nothing is ever confirmed."** The conclusion is right; the
  citation is not. `"trials"` appears in exactly one shape (`"trials":[]`, inside `node_evaluated`)
  and only in three task-arms, so it is not the universal marker it was quoted as. What IS
  universal is that **no score in the corpus was measured twice** — item 6.

---

## 1. The loop times the wrong size

    OPEN[probe-timing-runs-at-an-invented-scale]
    proof:absent:instance_size@looplab/tools/run_tools.py

**Measured.** `convex_hull`'s dataset is `convex_hull_T100ms_n267021_size100_train.jsonl` —
**n = 267 021**. The timing probes that decided its champion ran at **n = 100, 1 000 and 10 000**.
The champion's hot line materialises an `(n, 4)` float64 cross-product matrix:

```python
cross = (edges[:, 0][None, :] * (points[:, 1][:, None] - quad[:, 1][None, :])
         - edges[:, 1][None, :] * (points[:, 0][:, None] - quad[:, 0][None, :]))
strictly_inside = np.all(cross > eps, axis=1)
```

At n = 1 000 that matrix is **32 KB** and never leaves L2. At the real n it is **8.5 MB** and
streams from DRAM. The probe reported `quad+scipy ratio 1.45` and the shipped solver scored 1.09
against an arm that used four sequential `(n,)` passes. **The measurement was real and the regime
was invented**, so it predicted the wrong winner.

**Why the scale was invented:** every `data_schema` / `data_profile` / `read_asset` call on these
tasks returns *"no data assets at all"* — the instances live only on the evaluator — so the agent
had no source for n and picked round numbers. Its own research memo says so in as many words:
*"no data assets (instances exist only on the evaluator)"*.

**Fix.** The evaluator knows the shape; the agent must not have to guess it.

1. Have the bridge emit an instance profile beside the score — count, and per-field shape/dtype/size
   for the largest instance — and surface it in `read_experiment` and `data_schema` rather than
   returning "no data assets" for a source-code task.
2. Until that exists, make `run_probe`'s timing helper **refuse a size argument that no artefact
   supports** and say where a real one can be read.
3. Cheapest first step, no engine change: put the dataset filename (which already carries `n267021`)
   into the task card that `make_task.py` writes.

---

## 2. Two thirds of the budget is spent proving an idea is new

    OPEN[novelty-gate-costs-two-thirds-and-decides-nothing]
    proof:absent:novelty_budget_share@looplab/engine/novelty.py

**Measured across the eight task-arms**, attributing every `llm_usage` to the enclosing
`phase_progress`:

| phase | spend | share |
|---|---|---|
| `novelty` | **$5.324** | **66.3 %** |
| `propose` | $2.275 | 28.3 % |
| everything else (incl. every evaluation) | $0.430 | 5.4 % |

24 novelty phases, **16 538 s = 4.6 h** of wall clock. They produced **6 `novelty_rejected`
verdicts, and all 6 carry a `repropose` action** — every rejection was overridden and the idea was
built anyway. The gate consumed two thirds of the money and changed nothing that was built.

**19 node evaluations for $8.029 — $0.42 per measurement.** The counterpart arm bought 54–57
full-dataset evaluations for $1.00 ($0.0175 each) and won two tasks purely on micro-optimisations
that only a cheap measurement can find.

**Fix.**

1. **Cap the gate by budget share, not by satisfaction.** A hard ceiling (5 %? 10 %?) after which
   novelty returns "unknown, proceed" is strictly better than the current behaviour, which is to
   spend two thirds and then proceed anyway.
2. **Make a rejection binding or remove it.** A verdict that is always overridden is a tax. Either
   `novelty_rejected` blocks the build (and the proposer must produce something else), or the phase
   is demoted to an advisory note produced *inside* `propose` at no extra call.
3. **Re-target the money at evaluation.** The loop's own scoreboard shows the exchange rate: at
   $0.0175/eval the current novelty budget buys ~300 additional measurements.

---

## 3. The plan is stored; the artefact is what ran

    OPEN[stored-plan-diverges-from-shipped-artefact]
    proof:absent:plan_superseded@looplab/agents/unified_agent.py

On `count_riemann_zeta_zeros` the `node_repaired` triage prose plans a capitulation — *"call
`mp.nzeros(t)` directly… yields a valid scored submission (speedup ~1.0)"* — while the `files`
payload on that same event is the `_fp.siegelz` port that scored **6.0212**. On `discrete_log`,
`card-1` prescribes Pollard rho and node 1's code forces BSGS.

Anything that reads the cards rather than the code draws the wrong conclusion — and the cards are
what the next phase, the research memo and the final report all read.

**Fix.** After a build, reconcile: diff the plan's stated approach against the committed files and
either rewrite the plan or record `plan_superseded` with both. Cheap version: stamp the card with
the sha of the files that were actually written, and make any consumer that quotes a card also
quote that sha.

---

## 4. The validity verdict exists, is good, and is off the default path

    OPEN[validity-verdict-not-on-the-default-read-path]
    proof:absent:no_speedup@looplab/tools/run_tools.py

`score.log` carries the best failure record in the campaign: reason, instance counts, validity
percentage, and the task's own ranked `is_solution` rejection messages. On `spectral_clustering`
that verdict said **98/100 valid** with two named hack-detector messages.

What the loop was shown on its default path was `Best so far: node 0 metric=0.0`,
`1 experiment(s), 0 failed`, and a concurrently-written memo asserting the experiment "is still
pending, so there are no measured results yet". It responded with a *speed* hypothesis. The verdict
lives behind `read_logs`; `read_experiment` renders only the metric.

Note the three contradictions are each independently wrong: an invalid node is **not** `0 failed`,
and an experiment with a `score.log` on disk is **not** pending.

**Fix.**

1. Fold `no_speedup` (reason, `validity_pct`, the ranked messages) into `read_experiment`'s output
   and into the board line, so a zero always arrives with its cause attached.
2. Count an invalid node as **failed** in the run summary.
3. Build the research memo from state at generation time, not from a snapshot taken before the
   in-flight evaluation lands.

---

## 5. A completed evaluation is discarded when the ceiling lands

    OPEN[scored-node-lost-when-the-budget-ceiling-fires]
    proof:absent:drain_inflight_evaluation@looplab/engine/orchestrator.py

**Measured:** `integer_factorization` has 4 node directories, 4 `score.log` files and **3**
`node_evaluated` events; `spectral_clustering` has 2, 2 and **1**. In both cases the evaluation
**finished and wrote its score to disk**, and the run ended on the spend ceiling before the event
was recorded — so the loop never saw a result it had already paid for.

**Honest bound on the damage:** the lost scores were 4.0958 (champion 8.3255) and 0.0 (champion
0.0). Neither would have changed a champion. The mechanism is real; this campaign it cost nothing.

**Fix.** The ceiling check belongs before *starting* an evaluation, not before recording one. On
`BudgetExceeded`, drain any evaluation already in flight and fold its result before finalising —
the work is paid for either way.

---

## 6. Nothing is measured twice, and the champion is chosen from single draws

    OPEN[champion-chosen-from-unrepeated-measurements]
    proof:absent:trials@looplab/engine/champion_caveats.py

`evaluate.py` already writes `"trials": res.trials or []` — the mechanism exists. It is never
populated, and `champion_caveats.py`, which is what guards a champion switch, does not read it.
No score in the corpus was measured more than once. The counterpart arm re-scored its own
byte-identical file 30 times on `integer_factorization` and the spread was **8.02–13.81
(CV 12.4 %)** — so on that task a single draw carries roughly ±25 %, and both arms' numbers are
inside each other's noise. Our loop selects `exploit best` over single draws of a quantity that
noisy.

**Fix.** Re-score the incumbent and the challenger together, k times (k = 3 is probably enough),
before a champion switch, and record the trials. Where the metric's own variance exceeds the
proposed improvement, the correct action is *another measurement*, not another node — which is also
the cheapest action available (item 2).

---

## 7. A run that ends on the ceiling is recorded as an error

    OPEN[spend-ceiling-recorded-as-run-error]
    proof:absent:budget_exhausted@looplab/engine/finalize.py

Five of the eight task-arms ended with `run_finished {"reason": "error", "error": "LLM spend
ceiling reached: $1.00xx of the $1.0000…"}`. Reaching the budget is the **designed** terminal state
of a budgeted run; recording it as an error makes a healthy run indistinguishable from a crash, and
the campaign driver had to learn the difference from the exit code instead.

**Fix.** A distinct `reason: "budget_exhausted"`, with the error text kept as detail.

---

## 8. Nothing is retained between runs

    OPEN[no-lesson-survives-a-run]
    proof:absent:write_lessons@looplab/engine/claim_steward.py

**Measured:** every task's `memory/` holds `lessons.jsonl.lock` and **no `lessons.jsonl`**;
`knowledge/` is empty in all eight. The `spans.jsonl` shows `lessons_distill`, `lessons_refresh`
and `lessons_reconcile` running 11 times each on `edge_expansion` alone — the machinery runs and
writes nothing.

Two insights worth keeping were lost this way: that `mpmath.mp.prec` assignment is refused by the
arena's rules checker (learned by paying $0.0597 and 306.7 s for a rejected node), and the
`_fp.siegelz` guard idiom that produced the campaign's only 6x against a mature library.

**Fix.** Find out why the distil step produces no file — a lock without a payload suggests the
write path is failing silently — and make the absence loud. Then make a rules refusal a first-class
lesson, since it is the cheapest possible thing to remember and the most expensive to rediscover.

---

## 9. What is GOOD and must not be lost in the fixing

Every fix above trims something. These four are the reason the loop won what it won, and none of
them should be traded away for throughput:

1. **It reads dependency source.** `read_installed`/`grep_installed`, **81 calls**. The only move in
   either arm's record that treats the reference as *source* rather than a black box — importing
   `mpmath.functions.zetazeros` internals and applying mpmath's own guard idiom where the library
   omitted it — produced the 6.08 on `count_riemann_zeta_zeros`. The counterpart arm has no such
   tool and shipped the reference verbatim (1.04).
2. **It executes and reads stdout.** `run_probe`, **492 calls**. On `edge_expansion` a 0.16-second
   probe answered in 40 seconds a question the counterpart arm burned 83 % of its budget failing to
   answer — because its equivalent output never reached the model.
3. **The evaluator overrules the narrative.** Four consecutive research memos on `edge_expansion`
   declared the champion's formula wrong; the champion shipped because the measurement said 27.79.
4. **It derives the algorithm before it measures.** The `kcenters` memo at minute 13 contains the
   full winning design — integer-scaled numpy Floyd–Warshall, binary search over distinct
   distances, exact dominating-set search — including the observation that `is_solution` enforces
   *exact* optimality so correctness is binary. The counterpart arm reached the same place
   empirically 40 messages and $0.19 later.

---

## The two the CAMPAIGN found, not the transcripts

## 9. A ceiling is only as honest as the usage frame

    OPEN[a-ceiling-only-as-honest-as-the-usage-frame]
    proof:absent:synthesised_usage_frame@benchmarks/meter/proxy.py

**Measured 2026-08-26, mid-campaign.** The two arms were given the same `$1.00` per task. They did
not spend the same `$1.00`.

| | task-arms finished | median metered spend | max | over ceiling by >5% |
|---|---|---|---|---|
| arm A (AlgoTuner) | 15 | $1.011 | **$2.009** | 5 attempts, **3 of them the SCORED attempt** |
| arm B (LoopLab) | 20 | $1.003 | $1.011 | 0 |

The cause is not thrift and not greed. It is *what each loop is able to see*:

* AlgoTuner prices a call from the response's `usage` block. A stream the gateway ends **without**
  a usage frame therefore costs its ledger **nothing**, however many tokens crossed the wire.
* LoopLab prices from the metering proxy, which sums forwarded deltas. The same aborted stream
  costs it exactly what it cost.

`rbf_interpolation` is the clean specimen. Between 17:51 and 02:48 it made **seventeen consecutive
calls**, each running ~1808 s and forwarding ~220 000 tokens, each ended upstream with no usage
frame. Nine hours, **$1.006**, no usable output. Its own log then closes the case at 03:46:
`Spend limit of $1.0000 reached. Current spend: $1.0025` — while the meter had it at **$2.009**.

Ninety-seven arm-A calls (4.6 % of them) carry `stream_aborted`, and they hold **25.8 %** of arm
A's money. Arm B has $1.114 of the same aborted streams — counted, so they consumed budget like
any other call instead of arriving free.

**Why this is LoopLab's problem and not only AlgoTuner's.** It is not: LoopLab's ceiling held. What
is ours is that **the comparison table printed those pairs side by side and said nothing**, so a
pair where one side silently drew twice the budget read as a matched pair. Fixed in this commit:
`compare_arms.py` now reports metered spend per arm and names every task-arm that drew more than
its ceiling, with how much of it was invisible to that loop's own ledger.

**How we would fix the underlying thing.** The proxy already computes `estimated_from_deltas` when
upstream dies; it just never tells the client. Appending a synthetic `usage` frame to the client's
stream on abort would make any usage-frame-priced loop — AlgoTuner, and anything else pointed at
this gateway — see the money it actually spent. **Not applied while a campaign is running**:
changing the meter mid-run changes the ruler. It is the first thing to do after.

**A trap this cost us, recorded because it nearly shipped.** The first version of the check summed
a task across *attempts*. Two gateway outages had forced whole task-arms to be re-run, and those
abandoned attempts sit in the ledger with their scores already discarded — so the check reported
**nine** offenders, inventing `convex_hull` ($2.077 across two attempts, neither above $1.02) out
of nothing. Keyed by the attempt the `.done` marker credits, the real count among scored arms is
**three**. `tests/test_algotune_compare_arms_reports_real_spend.py` carries the wrong version as
its falsifier.

## 10. We took away the ruler and it built a fake one — CLOSED 2026-08-26

The marker is deleted rather than re-pointed: both halves shipped. `make_task.py --full-context`
now (a) states the measured instance shape in the goal and (b) pins an `eval_train` command that
runs the real evaluator on the train split against the Developer's staged files. `campaign.sh`
needed no change -- `MAKE_TASK_ARGS` is a pass-through, so the new arm is one environment variable.
Guarded by `tests/test_algotune_full_context.py`, whose nine cases each die under a targeted
mutation (eight tried).

It is OFF BY DEFAULT and that is not timidity: it CHANGES THE GOAL CARD, which is the measurement.
The twenty arm-B numbers already on disk were produced without it and must not be put in one table
with numbers produced under it.

What was NOT adopted, and why, because the obvious version of this fix is wrong: the train split is
on this machine and mounting it read-only was the first design. It fails twice. **Parity** -- the
reference agent never reads those files either; it has `eval_input` and `eval` and no path to the
dataset, so handing ours the instances is MORE than parity in the one direction that matters, since
the champion is graded on held-out instances from the same generator. **Cost** -- four of the twenty
tasks keep their arrays outside the jsonl in a `_npy_data/` directory holding BOTH splits (200
files, 816 MB on `convex_hull`); mounting it leaks test, and materialising only the train half is
~408 MB per node against a loop that evaluates nodes concurrently. What the agent actually lacked
was the SHAPE and a way to MEASURE.

The original finding, kept because it is the evidence:

Item 1 says the loop times probes at an invented scale. This is **why**, and it is not the model's
idea. Our own task text tells it, in capitals:

> YOU CANNOT MEASURE YOUR OWN SCORE, AND YOU ARE NOT MEANT TO. The instances you are graded on are
> not on this machine and you cannot generate them […] timing your own guesses against invented
> inputs measures something else — your guess about the input.

Measured against what the arena gives **its own** agent on the same tasks (2026-08-25/26 logs):

| what arm A gets | kcenters | rbf_interp | edge_exp | int_fact | convex_hull |
|---|---|---|---|---|---|
| `Speedup: X` + `Valid Solutions: Y%` on the TRAIN set | **61** | 38 | 17 | 52 | 60 |
| `eval_input` — its solver on a real instance, real timing | 207 | 429 | 296 | — | — |
| `profile` / `profile_lines` | 58 | 101 | 194 | — | — |

AlgoTuner re-runs the real evaluation after edits and hands the agent back **the grading metric
itself**, dozens of times per task. Ours is told the metric is unknowable and to stop guessing.

So the sentence is not wrong — timing an invented input really does measure the guess — but the
conclusion we drew from it was. The fix is not to warn harder. **The baseline gets a train-set
feedback loop; we removed ours and left the agent nothing to steer by.** Whatever we believed we
were protecting (train overfitting, evaluation cost) we bought by handing the comparison arm an
instrument the reference implementation has as standard.

Nothing in AlgoTune's protocol forbids it: train is what the agent is meant to optimise against,
test is what it is finally scored on, and both arms are already scored on test only. Describing the
instance distribution to the agent — the thing that would at least make its probes the right size —
is strictly LESS than what the reference agent is handed.

**How we would fix it, cheapest first.**
1. Put the instance shape in the task text (n, dtype, count, generation params — all of it is in
   the arena's dataset descriptor already). Costs nothing, and turns item 1's invented scale into
   the right one.
2. Give the loop a **train-subset** evaluation it can call — the bridge already runs exactly this,
   `looplab_eval.py --subset train`, and the baseline cache makes repeats cheap. Rate-limit it per
   node rather than forbidding it.
3. Keep the final number on **test**, as now, so the train loop cannot launder itself into the
   reported score.

Until 1 is done, every `run_probe` timing in every arm-B transcript is measuring a number the agent
made up, and we told it to.

---

## 11. Order to fix in

By measured cost, not by how bad each sounds:

1. **Item 2** (novelty gate, 66.3 % of budget) — the largest single recovery available, and it
   needs no new capability, only a ceiling.
2. **Item 1** (probe scale) — cheapest real fix: the size is already in the dataset filename.
3. **Item 4** (verdict off the default path) — one task lost outright to it.
4. **Item 6** (unrepeated measurements) — until this is fixed, no margin under ~25 % on a
   randomised task means anything, ours or anyone's.
5. Items 3, 5, 7, 8 — correctness of the record rather than of the result; cheap, and each one makes
   the next campaign's evidence readable.
