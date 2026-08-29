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

**CLOSED 2026-08-26.** The marker is deleted, not re-pointed, and the proof it carried is
withdrawn deliberately: it named `looplab/tools/run_tools.py`, and putting instance-size knowledge
into the generic probe tool is the wrong place for it -- that tool serves every task type, most of
which have no such thing. The owner ruled it out of scope explicitly.

What was actually wrong was the goal card, and that is fixed: `make_task.py` states the measured
instance shape and pins `eval_train`, and since this commit BOTH ARE THE DEFAULT. The reference
agent is shown the graded metric on the train split 17-61 times per task; withholding it from ours
was never a neutral default, it was a handicap applied to one arm, and inventing sizes was the
loop's rational answer to it. `--no-full-context` still reproduces the card the measured arm ran on.

The original finding, kept as the evidence:

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

**REFUTED 2026-08-26, both halves, and re-measured independently before this was written.** The
marker is deleted. What replaced it is a smaller, real defect and one new open item.

**It does not cost two thirds. It costs 0.6 %.** Summed from generation spans across all twenty
task-arms: `card_build` $6.1620 (34.8 %), `propose` $5.2500 (29.7 %), `plan` $3.3543 (19.0 %),
`novelty` **$1.3151 (7.4 %)**, `deep_research` $0.8352 (4.7 %). And 7.4 % is still the wrong
number for the gate, because 89 % of what carries that label is not the gate. Walking every
generation's `input_from` chain back to the system prompt that ROOTED it: the adjudicator ("You
judge experiment NOVELTY…") is **$0.1141 over 231 calls — 0.65 % of the run** — while $1.1758 over
257 calls is the Researcher and its claim-verifier, i.e. the whole second proposal the gate BUYS
when it rejects one.

**Where "two thirds" came from, reproduced to the cent.** The original reading carried the last
`phase_progress` with `status="started"` forward over every subsequent `llm_usage`. Only `propose`
and `novelty` emit that beacon, and `novelty` is the last to start before the loop moves into
`card_build`, `plan`, the build, the evaluation, research, lessons and the report — none of which
emit one. So every dollar spent after the gate closed banked to it.

**And it decides.** Eleven `novelty_rejected` events (not six): ten `reproposed`, one
`budget_exceeded`, zero kept. `reproposed` is stamped only when the proposal digest CHANGED, and
recomputing that digest over each `node_created` payload confirms it in **ten of ten** — the idea
that got built is never the idea the gate rejected. The verdict is binding.

**A cheap similarity check will not replace it**, and that is measured rather than assumed: scoring
proposed-against-nearest-prior offline over all 49 adjudicator prompts, TF-IDF cosine does not
separate (duplicates 0.466–0.779, novel 0.344–0.716); the best oracle threshold reaches 0.80
accuracy only by admitting 4 of 11 duplicates and rejecting 6 of 38 good proposals. The duplicates
are `bincount` vs `csr matvec`, `argsort` vs `zip/sort` — textually distinct descriptions of one
algorithm. `options.py` ships `novelty_semantic` off; that default is now measured.

### 2a. FIXED[repropose-billed-to-the-gate-that-asked-for-it] `proof:present:_repropose_phase@looplab/engine/novelty.py`

The real defect is one span deep. `_paid_progress` opens the `novelty` span so the gate's money is
attributable at all, and `Tracer.span` stamps `phase=<innermost open operation>` on every
generation beneath it — so the re-proposal, having no span of its own, inherited `novelty`. A phase
whose recorded price is ten times its own work is a measurement trap, and this campaign fell into
it twice: this section, and `shared.py::_paid_progress`'s own $1.77 note. `_repropose_phase()` now
opens a nested `repropose` span. Span only, no `phase_progress` beacon — the loop IS still inside
the gate, and that table's exact rows are pinned by `test_end_to_end` / `test_settled_width_pins`.
It opens no call and cannot change a verdict.

### 2b. The item this one uncovered

**CLOSED 2026-08-26, and the location I recorded was WRONG.** The headline re-derives exactly --
`llm_usage` $20.0081 over 6819 calls against $17.6867 over 5903 generation spans, a $2.3214 gap --
but "almost all of it is `card_build` and `hyp_prioritize`" does not survive. Both open spans and
are fully accounted (`card_build` $6.1620 / 1755 generations; `hyp_prioritize` is a real span name
at `search/foresight.py:391`, $0.2908 / 297). **Zero** unspanned rows belong to either. That
sentence came from a summary I passed on without checking the one claim in it I could have checked.

Attributed to the operation actually open around each row, the gap is FOUR unrelated defects and
sums to $2.3214 with nothing left over:

| site | calls | $ | share | cause |
|---|---|---|---|---|
| concurrent deep research | 817 | **2.1921** | 94.4 % | `_research_attempt_step` is shared by the serial cadence and BOTH concurrent seams; only `_run_deep_research` opened the span |
| `_tag_hypothesis_concepts` | 88 | 0.0211 | 0.9 % | pays after the `concept_coverage` span beside it closes |
| ceiling-aborted calls | 36 | 0.1015 | 4.4 % | the span exists and carries no cost: `CostAccountant.add` pays, emits the row, then raises `BudgetExceeded` before the caller can stamp it |
| finish report | 11 | 0.0067 | 0.3 % | the span WAS opened and never reached disk — a different defect, §2c |

**What it changes about the loop.** Deep research filed $0.8352 (4.7 %) and actually cost about
**$3.03 -- roughly 15 %, the arm's fourth-largest consumer, ahead of the novelty gate.** Every
per-phase conclusion drawn before this was drawn over a channel missing its third-biggest line.

The docstring that caused it -- *"the tracer is not safe to write from the concurrent worker"* --
is false, and the counter-example sits in the same file family: `_maybe_merge_hypotheses` appends
`hypothesis_merged` from the very same `anyio.to_thread.run_sync` hop.

A third site was found BY INSPECTION rather than by measurement: `verifier_tiebreak.py` runs
between two span-opening siblings and opened none. `select_verifier` is off by default, so it
bought $0.0000 here and would first have surfaced in whichever campaign turned that knob on.

**The guard is a CONSERVATION test, not a per-site checklist**: every `llm_usage` row joins a
generation span, every such span carries a cost, and the two sums agree to 1e-9, with a
non-vacuity latch so a scenario that bought nothing cannot pass. Not an assertion in the tracer,
deliberately: the failure mode IS that nothing calls the tracer, and at the one place that sees
every paid call, "no span is open" and "nothing is traced" are the same observation.

### 2c. The one it uncovered

**CLOSED 2026-08-27, and the item's own NAME was wrong.** The marker is deleted. Nothing vanished
between close and flush: the span never reached the exporter's queue, because the exporter was
already terminal when the span opened. Cause established, and it is one frame, not a race.

**Re-measured first, over all thirty run dirs** (`runs-B` + `model-probes` + `fullctx-probe`, with
the crash-atomic `__looplab_event_batch_v1__` packets expanded): **15** `report_generated` rows name
a `span_id` that is in no artifact -- 11 under `runs-B`, exactly as recorded, plus `fxKcenters`,
`gpt56luna`, `opus5` and the fullctx probe. All 15 are `trigger="finish"`, and all 15 belong to a
run whose `run_finished` is the ceiling (`error` on the older arm, `budget_exhausted` after §7). No
run that ended otherwise lost one.

**The mechanism.** `Engine.run`'s `finally` retires the exporter -- one lifetime per run, terminal
so that a straggler closing later is REJECTED rather than appended behind a trace reset/clear. A
ceiling hit does not end there: it escapes `Engine.run`, and `cli/run_cmds.py::_run_engine_guarded`'s
outer handler then writes the terminal AND buys the finish report, several frames above that
`finally`. `AsyncJsonlSpanExporter.export` refuses the post-shutdown row and records the drop with
`durable=False` -- deliberately, so a dead exporter can never be resurrected as a receipt writer --
so the refusal leaves nothing on disk at all. Right for a background straggler; wrong for the run's
own terminal report, which is synchronous, on the main thread, and still inside the engine lock.

**Reproduced end to end before the fix**, real `Engine` + real exporter + a `_run_with_llm_broker`
that raises `BudgetExceeded`: `report_generated` carried `span_id=71bcb6b78f90bc75`, `spans.jsonl`
held zero rows, and the exporter's own counters read `dropped_shutdown: 1, loss_receipts: 0` -- the
corpus signature exactly.

**So the LIFETIME moved and the FENCE did not.** The owner that writes the terminal owns the trace:
`_run_engine_guarded` calls `Engine.defer_trace_retirement()` and runs `retire_tracer()` in its own
`finally`, inside the same lock scope `Engine.run` held. Nothing about the barrier, the
abandon-on-timeout or the writer guard changed.
`tests/test_finish_report_span_survives_the_ceiling.py` carries three cases and each dies under its
own mutation: dropping the deferral reproduces the missing span; dropping the retirement lets a
post-owner straggler onto disk; deferring by DEFAULT leaves every directly-driven `Engine.run`
(server, TUI, ~40 tests) with a live writer behind the lifecycle lock. Two existing tests were
scaffolding on the old shape and are updated to the real property -- the source pin now asserts
BOTH that `run`'s `finally` reaches the retirement AND that the retirement is still a bounded
`shutdown`, which a single relocated assertion would not have.

The original finding:

Eleven finish-report spans were opened, their ids reached the events, and the records are absent
from `spans.jsonl`, `.spans-append.jsonl` AND `trace.json`, with no exporter-loss receipt anywhere
in the corpus. The corpus splits cleanly 20 of 20: **all 11 runs that lost the span ended on
`BudgetExceeded`; all 9 that ended otherwise kept it.** Cause not established. It matters more than
its $0.0067 -- a barrier that returns while leaving an accepted span off disk is a hole in the
record, not an accounting rounding error.

The finding that uncovered it:

**$2.3214 across 916 calls — 11.6 % of arm B's money — appears in `llm_usage` events and in NO
generation span.** Spans account for $17.6867 of the $20.0081 the event log records. Almost all of
it is `card_build` and `hyp_prioritize`. Every per-phase question asked of the span channel is
therefore answered over 88 % of the money, and the missing 12 % is concentrated in the single
largest consumer. This is the same class of error as 2a — a cost channel that is silently partial —
and it is the one to fix before any further conclusion is drawn about where the loop's money goes.

**What survives of the original complaint.** Nothing here says the spend is well allocated:
19 node evaluations for $8.029 on the 8-task corpus is $0.42 per measurement against the reference
arm's $0.0175. But the money to re-target is `card_build` (34.8 %) and `plan` (19.0 %), not the
gate.

The original finding, kept as the evidence that was refuted:

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

**CLOSED 2026-08-27, and the status line above it was STALE, not the code.** The marker is deleted.
The line it replaces — *"STAYS OPEN — the build half is now RECORDED, the repair half is not"* —
was written by `4b771574` at 09:01 on 2026-08-26. The repair half landed four hours later in
`5f253594` (13:02), and that commit updated only §9a/§9b of this document. §4a was left stale by
the same commit for the same reason. A status line nobody re-derives is wrong in both directions;
this one had been wrong in the "still broken" direction for a day.

**Verified rather than taken from a commit message.** `engine/repair_verify.py::repair_attribution`
replayed over the corpus's single real `node_repaired` row — `runs-B/count_riemann_zeta_zeros`
node 0, the only one in thirty run dirs — against that node's own `node_created` pre-image:

```
{"prose_authored": "before_repair",
 "wrote": [{"path": "solver.py", "kept": 0.725}],
 "deleted": [], "unattributed": [], "named": ["solver.py"], "unnamed": []}
```

under a rationale reading *"the minimal safe repair … is to call `mp.nzeros(t)` directly, removing
all tampering"* — while the shipped file KEPT 72.5 % of the mpmath port that rationale prescribed
deleting, and scored 6.0212. That is the section's own specimen, and the divergence is now a number
on the row.

**And the wiring is falsified, not just the function.**
`tests/test_repair_attribution.py::test_the_engine_stamps_it_on_node_repaired` drives a real inline
repair end to end; deleting `"attribution": _attribution` from `engine/evaluate.py`'s payload turns
it red with `KeyError: 'attribution'` (re-run here, not taken on trust).

**The proof token was mis-pointed from the start and is deleted rather than re-pointed.**
`plan_superseded` was never going to appear in `agents/unified_agent.py`: that file authors the
triage PROMPT, and the reconciliation lives in `adapters/repo_developer.py::plan_step_attribution`
(build half) and `engine/repair_verify.py::repair_attribution` (repair half). Nor is there a
`superseded` boolean on the repair side, and that absence is a decision recorded at the site: the
one corpus specimen would not trip any honest byte-anchored rule for it — the prose names
`solver.py` and the repair wrote `solver.py`, and the contradiction is in the MEANING. A flag that
misses the case it was built for is worse than the gap.

**One sibling row noted and deliberately not changed**, so a later reader does not think it was
missed: the `salvage_cause_fix` `node_repaired` row (`engine/evaluate.py:1731`) also ships `files`
beside a sentence, and carries no `attribution`. It is not an instance of this item — that sentence
is ENGINE-authored at record time, not a model prescription written before the session — and the
row already carries `changed`. Zero such rows exist in the thirty-run corpus.

**The item was right and too narrow: there are TWO plans, and the durable record holds the wrong
one.** The CARD's rationale is authored by the Researcher BEFORE any repo read, and nothing rewrites
it afterwards. The DEVELOPER'S plan is authored AFTER the plan phase has read the source and run
probes — it is the plan the code actually came from — and it is a local Python list handed to
`_run_step` and then dropped. Measured: 63 of 70 builds produced one, and **`propose_plan` appears
0 times across all twenty event logs**. It survives only as an unkeyed tool-call payload inside a
span attribute.

Three more things the corpus says, all verified independently: **0 `plan_step` spans** — every
step session collapsed into one `card_build` band, while the phase list has claimed "each step its
own trace block" since it was written; **0 `stages` spans** — the operator declares `eval.stages`,
so that phase short-circuits for every build; and only **6 `plan_steps_failed` spans** exist, so
the silent steps are almost entirely unexplained.

Of the 46 builds whose plan had two or more steps: **46 steps (41 %) wrote nothing at all**, and
**22 files were finished by a LATER step than the one whose title claims to produce them**, across
18 builds. Every one of the 46 carries at least one divergence.

`discrete_log` node 1 is the specimen, and it shows the loop WORKING. The card says "Pollard's rho,
O(1) memory". The plan phase then probed rho against BSGS at 2^37…2^46, found rho slower at every
size, and titled step 1 *"Implement solver.py with a BSGS-everywhere discrete_log"* — in its own
words, *"the winning move is the OPPOSITE of the hypothesis."* The shipped file sets
`RHO_THRESHOLD = 2 ** 46`. Only the record is wrong: 1.018 reads as a verdict on rho when it is a
verdict on BSGS. And that run's champion, node 0 at 1.186, has a step titled *"Add three subgroup
solvers with dispatch"* that **wrote nothing** — the shipped solver has one plain `_bsgs` and no
fallback, so its whole correctness tier exists only in a tool-call payload.

**Design consequence, not a bug — so record, don't prevent.** The plan is a proposal, the artefact
is the truth, and a Developer that overrides its plan on measured evidence is doing its job. Each
step now runs in its own `plan_step` span; the working set is diffed BY CONTENT around each step,
so an `edit_file` rewriting identical bytes is not authorship; and one `plan_steps` span carries
the reconciliation — per step what it wrote, whether it superseded an earlier one, whether it was a
silent no-op — plus `authors` (shipped file → the step that LAST wrote it) and `unattributed`
(shipped files no step touched). 1808 developer/plan/card tests green; crediting the first writer
instead of the last, or testing presence instead of content, each turns the new tests red.

The original finding:

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

**PART-FIXED 2026-08-26, the rest 2026-08-27.** The marker was deleted for fix 1; fixes 2 and 3 in
the body below (an invalid node counted as `0 failed`, and a memo built from a pre-eval snapshot)
were re-opened as §4a. §4a is now closed and the memo half is §4b.

**In plain words, because the section did not manage it.** When our solver is scored, the bridge
prints one JSON line. If the solver is wrong on even one of the hundred instances the arena refuses
to time it and the line carries `speedup: 0.0` — but never alone. Beside the zero sits a
`no_speedup` object: the failure class, how many instances were valid, and verbatim the messages
the task's own `is_solution` logged when it rejected our answers. That object is good, it reached
disk, and it was in the fold every time. **The problem was which tool showed it.** An agent asking
"why did my last experiment score that" calls `read_experiment` — the tool whose own description
says "read one experiment's full detail". It printed `metric=0.0` and stopped. Only `read_logs`
rendered the reason, and an agent that already has the number has little reason to make a second
call. So the run read `0.0` as a verdict on its IDEA — approach wrong, discard — while the record
said the approach was right and the implementation had two bad edge cases.

**Measured over the twenty arms.** Eleven `score.log` records are zeros carrying a reason; nine of
them reached a `node_evaluated` row. On those nine: **61 `read_experiment` calls returned the
reason 0 times, and 32 `read_logs` calls returned it 32 times.** Corpus-wide the same shape, 574
against 293. On `spectral_clustering` — four `read_experiment` calls on the zero node and **zero**
`read_logs` calls in the entire arm — the verdict "98/100 valid" plus two named hack-detector
messages reached **no tool output in that run at all**.

`digest.metric_account` now reads the `no_<metric key>` block off the node's final metric line and
renders it at three sites: `read_experiment` (600 chars — the longest real block renders at 576 with
all three ranked messages whole, where the 300 that `failure=` gets cuts five of nine short), the
`list_experiments` line, and the always-on working set (~84 chars). Registered as signal route
`scored_eval_reason`. Render-only: no decision path references it.

**The `no_` prefix is load-bearing, and that was found by measurement, not design.** The first rule
was "any nested object carrying a `reason`" — which matched all 56 rows, because every healthy node
prints `subset_evidence.reason: patch_marker_present`. `no_<x>` matches the nine and none of the
forty-seven, and a mutation relaxing it reproduces that exact false positive.

**A correction to §0 of this document, which was mine.** §0 recorded that the claim "`read_logs` was
called 0 times on `spectral_clustering`" had been refuted — "13 times on that task". It had not.
All thirteen occurrences are the tool INVENTORY receipt (`read_logs=0`, a count of nodes) and the
advertised tool list inside prompts; the real call count is **zero**. That rebuttal was a `grep -c`
over `spans.jsonl` — the same class of error this document exists to catch. The separate
88-across-eight-arms figure stands.

**And one record is not an instance of this defect at all:** `count_riemann_zeta_zeros`'s
`speedup: null` rules violation travels `looplab_failure_reason`, fails the node, and
`read_experiment` already rendered `failure=`.

### 4a. CLOSED 2026-08-27 — the two counting halves

The marker is deleted. It covered THREE contradictions in one prompt and they closed in two steps,
so both are recorded here with their own measurements. (The third, the memo, is §4b.)

**Half one, the headline, landed 2026-08-26 in `5f253594` and this document was never updated —
which is exactly the drift the marker convention exists to catch.** `digest.metric_scored_invalid`
plus a third count on the working-set headline: 9 of 56 `node_evaluated` rows over 6 of the 20
arms carry a `no_<metric key>` block, `node_failed` fires zero times campaign-wide, and 61 renders
of a literal "0 failed" sat over a board holding an invalid experiment. Counted SEPARATELY rather
than as failed — calling it failed is the same untruth pointed the other way, and
`strategist.failure_rate` is a real decision path. A grep test keeps the predicate out of nine
decision modules.

**Half two, the champion line, lands now.** `agents/roles.py::_state_brief` opens every proposal
prompt with `Best so far: node N metric=<x>`, and `<x>` was a bare `0.0` whether the run measured a
genuine zero or the arena refused to time the solver at all — the two facts a proposer must not
confuse, rendered identically one line above the headline that had just learned to tell them apart.

**Measured over all thirty run dirs** (`runs-B` + `model-probes` + `fullctx-probe`, crash-atomic
packets expanded): **340 renders of that line, 16 of them naming a node whose own eval had refused
to score it.** Nine are the WHOLE of `spectral_clustering` — the arm never proposed anything under
a different champion, and the literal bytes in its `spans.jsonl` are `Best so far: node 0
metric=0.0 params={}` over a `score.log` reading 98/100 valid — plus 3 on `rectanglepacking` and 2
each on the `gpt56luna` and `sol1` probes. The `Refine from node N … metric=` line just below is
the same untruth from the parent's side; it names an invalid node **0** times in this corpus and
gets the clause anyway, because a fix that covers only the line that happened to fire is a fix that
reopens on the next corpus.

`digest.unscored_metric_clause` is the ONE spelling — the headline and the champion line are two
renders of one fact and are built in different packages, which is how two vocabularies for one
thing get into one prompt. Render-only, the boundary `metric_account` states and
`test_nothing_that_decides_reads_the_predicate` enforces: `state.best()` returns exactly the node it
returned before. Four mutations turn the new tests red, including the one that matters most —
relaxing the predicate to "the metric is 0.0" fires on 47 of the corpus's 56 evaluated rows instead
of 9.

The original finding:

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

### 4b. CLOSED 2026-08-27 — the memo that denies a result already on the board

The marker is deleted, and **the fix this section asked for was measured and does NOT work**, which
is the most useful thing here. Fix 3 above says "build the research memo from state at generation
time". Across **131** `research_attempted` receipts the snapshot's own node count disagrees with
the log **0 times**: the snapshot is already fresh when the provider call STARTS, and goes stale
while it RUNS. Re-folding one line earlier recovers nothing at all. A memo cannot see the future.

**What the record can do is stop presenting it as current.** `_results_since_snapshot` diffs the
evaluated nodes at RECORD time against the ones the memo was COMPUTED from and stamps
`snapshot_superseded` inside the memo payload — inside, because
`events/replay.py::_on_research_completed` folds only `d["memo"]` into `state.research`, so a fact
on the event envelope alone could never reach the prompt that quotes the summary. Engine-derived
and minted before the memo id, exactly like `verification`. `memo_snapshot_cue` then rides the
"Latest deep-research takeaway" line, UNGATED unlike the verdict cue beside it: without it the
prompt contradicts itself, since the working set two lines up already shows the result the memo
denies. Empty when nothing was superseded, so the 41 memos that overlapped nothing render the
historical bytes.

Four mutations turn the new tests red: not recording the receipt; counting the whole board instead
of the delta against the memo's own snapshot (which would make every memo on a mature run claim to
be stale); dropping the prompt cue; and removing the node-id bound, which lets a hostile receipt
spend the prompt line. 1514 research/memo/advisory/roles/prompt/replay tests pass.

The original finding — `spectral_clustering`'s own event log, timestamps relative to `run_started`:

| seq | t | event |
|---|---|---|
| 128 | 2052.8 s | `node_eval_started` node=0 |
| 130 | 2052.9 s | `research_attempted` trigger=cadence at_node=1 |
| 144 | 2109.1 s | `node_evaluated` node=0 **metric=0.0** |
| 155 | 2365.7 s | `research_completed` trigger=cadence at_node=1 |

The memo was COMPUTED from the state at 2052.9 s and RECORDED **256 seconds after node 0's result
was on disk**, opening *"experiment #0 (deterministic-baseline-replication) is still pending, so
there are no measured results yet — the memo's 6 claims are all UNSUPPORTED because they cite no
experiment."* `_state_brief` then pushes that sentence into every later prompt as the "Latest
deep-research takeaway", directly beneath a working set showing the result it denies.

**Measured over the thirty run dirs: 78 of 119 completed memos (65.5 %)** were appended after at
least one `node_evaluated` their snapshot could not contain, on 28 of the 30 runs.

---

## 5. A completed evaluation is discarded when the ceiling lands

**CLOSED 2026-08-26.** `Engine.run` now drains the in-flight evaluation before the ceiling tears
the run-scoped eval task group down (`orchestrator.py::_drain_inflight_evaluation`), and the
speculation-off path defers the same stop until its evaluations have joined
(`orchestrator.py::_DeferredBudgetStop`). The run still ends on the ceiling, with the same
exception and the same `run_finished {"reason": "budget_exhausted"}`; what changed is that the
evaluation it overlapped is now IN the log it stops over.

**The measurement was larger than this section recorded.** Re-derived across all twenty task-arms
under `runs-B`, not the eight-arm scored corpus: **five** arms lost a scored node, not two, and
five scores in total.

| task-arm | node dirs | `score.log` | `node_evaluated` | lost score | champion |
|---|---|---|---|---|---|
| `integer_factorization` | 4 | 4 | **3** | 4.0958 | 8.3255 |
| `spectral_clustering` | 2 | 2 | **1** | invalid results → 0.0 | 0.0 |
| `max_clique_cpsat` | 7 | 7 | **6** | invalid results → 0.0 | 31.664 |
| `min_dominating_set` | 3 | 3 | **2** | 1.0804 | 7.2265 |
| `multi_dim_knapsack` | 5 | 5 | **4** | 2.8004 | 2.8586 |

The last row is the near miss the two-arm reading could not see: 2.8004 against a champion of
2.8586, inside the ±25 % single-draw noise item 6 measures on this benchmark. All five event tails
are the SAME five rows — `node_eval_started` → `workspace_seeded` → `research_attempted` → one
research `llm_usage` → a long silence → the ceiling — which is what identifies the mechanism rather
than merely counting its victims.

**The mechanism, exactly.** The last paid call in every one of the five is the *overlapped deep
research* `_spawn_research` starts beside the evaluation. It crosses the ceiling and raises
`BudgetExceeded` out of the CardSession's `bg_task_group`, while the evaluation — which since F1f
lives in the RUN-scoped group `Engine.run` owns, a strictly outer one — is still inside
`anyio.to_thread.run_sync(self._run_eval, …)`. That hop is `abandon_on_cancel=False`, i.e.
shielded, so the cancellation cannot abandon it: the subprocess ran to completion and wrote
`score.log` (24–190 s after the ceiling fired, per the timestamps). The cancel was then delivered at
the first checkpoint *after* it returned, which sits between the eval finishing and its single
`EV_NODE_EVALUATED` append in `engine/evaluate.py`. Cancelling saved nothing — the compute was
already spent — and lost the only durable record of it.

**Not weakened, and that is tested as hard as the fix.** The drain starts no new work and can reach
no LLM call, both dispatch branches test for a deferred stop BEFORE starting an evaluation (in the
parallel branch at the refill point, which is the only place every route to an admission passes
through), and `Engine.run`'s `raise` is untouched. Seven mutations — dropping the drain hook,
draining unconditionally, handing `_spawn_research` the raw task group, dropping the serial
admission gate, dropping the refill-point gate, never re-raising the deferred stop, and widening the
facade's `except` to `BaseException` — each turn `tests/test_budget_ceiling_drains_the_inflight_eval.py`
red.

The original finding:

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

**CLOSED 2026-08-26 as NOT A DEFECT — the owner's call, recorded with its reasoning so it is not
re-raised.** Repetition is a policy choice, and on this benchmark the policy is already paid for
elsewhere: every score is the aggregate over 100 instances, the champion is selected on train and
then scored again on a held-out test split, so a number that survives to the table has been taken
twice over two different sets of instances. Adding per-node trials would buy variance reduction on
a quantity that is already an average of a hundred, at a cost measured in the run's only scarce
resource.

The observation stands and may matter for a DIFFERENT task type -- one whose metric is a single
noisy measurement rather than an aggregate. It is written down here for that case, not for this one.

The original finding:

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

**CLOSED 2026-08-26.** `run_finished` now names the disposition `budget_exhausted`, with the
sentence kept in `error`. The measurement was worse than this section recorded: not five of eight
but **eleven of eleven** finishes under `runs-B` said `error`, and every one was the ceiling —
zero genuine failures in the whole campaign.

THE FIX AS THIS SECTION PROPOSED IT WOULD HAVE BROKEN THE ENGINE, and that is worth keeping.
`reason == "error"` never meant "this run crashed". It means "this terminal event was written by
`cli/run_cmds.py::_run_engine_guarded`'s outer handler rather than by the engine's own clean
finish", and six sites in `finalize.py`/`finalize_scope.py` key on exactly that to avoid stealing a
terminal intent. A distinct reason introduced without teaching them would have made a guarded abort
look like a clean engine finish. So the reason is distinct AND the class is now a predicate,
`is_guarded_abort`, with a test that fails if a seventh site ever spells the literal again.

It also closed a hole the old code could not see at all: `_run_engine_guarded`'s own docstring
records that anything raised inside the eval task group escapes as the GROUP's "unhandled errors in
a TaskGroup (1 sub-exception)", so a ceiling hit on the concurrent path reached the event with
neither its class nor its sentence. The search is now recursive through exception groups and cause
chains, depth-bounded so a constructed cycle cannot hang a terminal-event handler.

1276 finalization/budget/scope tests green; three mutations each turn the new tests red.

The original finding:

Five of the eight task-arms ended with `run_finished {"reason": "error", "error": "LLM spend
ceiling reached: $1.00xx of the $1.0000…"}`. Reaching the budget is the **designed** terminal state
of a budgeted run; recording it as an error makes a healthy run indistinguishable from a crash, and
the campaign driver had to learn the difference from the exit code instead.

**Fix.** A distinct `reason: "budget_exhausted"`, with the error text kept as detail.

---

## 8. Nothing is retained between runs

**CLOSED 2026-08-26**, and the item's own evidence was wrong in three places while its conclusion
was right for a different reason. Marker deleted.

**What the corpus actually says** (all 20 task-arms, re-counted independently): `lessons.jsonl`
exists for **four** tasks, not none, with one lesson each and four matching `lessons_distilled`
events. But `meta_notes.jsonl`, `cases.jsonl` and `skills/` exist for **zero of twenty**, and the
only finalize steps anywhere in the corpus are `begun` / `report_begun` / `report` / `abandoned` —
no `reflection`, no `case`, no `budget`, no `diversity`. Eleven runs reached finalization and all
eleven closed the same way.

**The mechanism, and it is not where this section looked.** `_recover_scoped_terminal` appends
`finalization_finished` — which clears `finalization_pending()` — and marks the scope abandoned,
while `_scope_is_effective_terminal` excludes the guarded-abort class by construction. So
`should_finalize` is False on that pass and on every later one, and `finalize_run`'s ENTIRE
checklist goes with it. Run-end reflection — the meta-note, the distilled cross-run lessons, the
auto-skill cards, i.e. everything a LATER run could read — lives in that checklist.

That made it the ORDINARY outcome rather than an edge case, because a budgeted run is supposed to
end exactly there.

**It is NOT fixed by naming the ceiling `budget_exhausted` (§7), and believing otherwise was the
trap.** That reason is in `GUARDED_ABORT_REASONS` deliberately: `reason == "error"` never meant
"crashed", it meant "written by the outer handler rather than the engine's clean finish", and six
protocol sites need the distinction. Renaming it changed nothing here. The reflection is now run
inside `_recover_scoped_terminal` instead, guarded so it can never wedge a terminal, gated on the
scope's own `reflection` marker so a resume neither re-spends it nor duplicates a note. Its two
effects were ALREADY allow-listed by `finalize_scope_quiescent` — the protocol had anticipated this
call site. That also repairs the case this section was nominally about: a genuine crash used to
cost the run its memory too.

**Three pieces of the original evidence do not survive.** "No `lessons.jsonl`" — false over twenty;
the eight-task sample happened to contain none of the four. "A lock without a payload suggests a
failing write" — false: those locks are taken by a READER,
`governance_health.py::project_governed_sources`, unconditionally. "The machinery runs and writes
nothing" — the eleven sub-millisecond `lessons_distill` spans on `edge_expansion` are the cadence
gate returning; where it opens, the span runs 4.8–8.7 s and the write succeeds.

**And one thing is not a defect at all.** No two task-arms share a store: `campaign.sh` gives each
its own `LOOPLAB_MEMORY_DIR`, deliberately, so arm B could not reach task 12 with eleven prior runs
to read while the reference arm has no equivalent. Cross-run transfer was never on trial here.
Left alone.

2100 finalization/budget/lesson/scope tests green; removing the reflection call turns the new tests
red, including one written for the `budget_exhausted` reason the ceiling actually writes — a test
covering only `"error"` would have gone on passing while every real budgeted run kept losing its
memory.

The original finding:

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

**PART CLOSED, PART REFUTED, PART STILL OPEN — 2026-08-26.** The marker above is deleted because
its proof was simply wrong: the proxy was ALREADY minting a synthetic usage frame and already
delivering it. I recorded as missing a thing that existed. Two measured reasons it bought nothing:

**The wire shape.** The frame put `usage` on the same chunk as a populated `choices` array. The
`stream_options.include_usage` convention — which every OpenAI-compatible client follows — reads
`usage` off the chunk whose `choices` is EMPTY. Measured against real litellm 1.97.0 from AlgoTune's
own venv: it reports `usage=None` for that shape, and with `include_usage` set it mints its own
`Usage(prompt_tokens=0, completion_tokens=0)` — a zero the proxy never sent. Arm B was unaffected
only because `core/llm.py` takes `usage` off any chunk at all.

**The prompt side was a false measurement, not an under-report, and it is the expensive half.**
Across arm A's 1 773 complete streams the prompt is **97.3 %** of metered spend ($13.93 of $14.31;
median prompt 42 698 tokens against median completion 537). Priced properly the aborts add **$1.20
to their $6.44 — 18.6 %**. So the old frame captured 0 % of an abort's true cost and the new one
captures ~81 %. No tokenizer exists in a stdlib-only proxy, so it counts prompt CHARACTERS exactly
and converts them with a ratio calibrated in-process from calls this gateway itself priced — 6.5 %
median error against `tiktoken` over 93 request-sized blocks of the campaign's own text, 15.2 % at
p90. With no priced call yet it returns 0 and says `unmeasured` rather than guessing.

### 9a. CLOSED 2026-08-26 — the half that actually cost the money

The marker is deleted rather than re-pointed: `abort_is_not_retryable` and a `--delta-ceiling` are
in `benchmarks/meter/proxy.py`, with `tests/test_meter_delta_ceiling_is_not_retryable.py` as the
falsifier. What follows is the finding, re-measured, and the decision.

**The numbers, re-derived.** `meter/meter.jsonl` at 9,235 rows: **134 aborted streams, $6.646 and
62.85 h**. The three numbers this section was written with reproduce exactly at the 131st abort —
$6.552 and 61.91 h — so they were right and the campaign has simply added three more since. 113 of
the 134 are arm A on the stream-adapted path; 21 are arm B streaming; 116 of them ended between
1,795 s and 1,825 s.

**The cut is not the cost; the RETRY of the cut is, and that is now measured rather than asserted.**
Grouped by `(arm, task, attempt)` in arrival order the 134 aborts form 44 consecutive runs, and four
of those runs are 23, 23, 16 and 15 long — **77 aborts on four task-arms**. Arm B's longest run is
2. Over the same log:

| counterfactual | aborts | wall saved | money saved |
|---|---|---|---|
| keep only the first abort of each run | 44 | **43.34 h (69 %)** | **$4.55 (68 %)** |
| 135,000-delta ceiling alone | 134 | 16.27 h (26 %) | $1.94 (29 %) |
| both | 44 | **48.67 h (77 %)** | **$5.19 (78 %)** |

So the multiplier is worth two and a half times the per-call cap that 9b proposed, and it is a
CLIENT loop: `AlgoTuner/interfaces/llm_interface.py` wraps the call in `for attempt in range(10)` /
`except (RateLimitError, APIError, APIConnectionError)`. Its logs show litellm's own classifier
getting it right — `LiteLLM API non-retryable error` — and the layer above overriding it on the same
exception, 72 times across the campaign's logs, with five runs reaching `Exceeded max retries (10)`.

**Nothing honest makes that loop stop.** Its only escape is a substring match on a payment/quota
list (`"402"`, `"insufficient credits"`, `"quota exceeded"`, …), so making an abort non-retryable by
STATUS or ERROR SHAPE means asserting something false about the account. And the status is not ours
to choose anyway: the 200 and the headers are on the wire long before the cut happens.

**What is honest is to stop the call being an error.** It is not one. The request partially
succeeded, 150k–245k tokens were generated, and this meter has already billed them. Delivered as
what it is — a truncated completion with a finish reason, a price and the `data: [DONE]` sentinel —
a loop that retries *errors* has nothing to catch. That is `abort_is_not_retryable`.

**The delta ceiling is in, and its job is not the 26 %.** It is what makes the refusal RELIABLE. The
proxy can only hand back a complete ending on a call it is still holding; when the gateway ends the
generation, what reaches the client is whatever the dying socket leaves behind — 31 of the 134 rows
carry a `BrokenPipeError` from trying to answer a client that had already gone. A cut the meter
chooses is complete and identical every time. 135,000 clears arm A's largest complete answer
(132,269 deltas) and arm B's (126,559), so it fires on nothing in the recorded corpus; the ceiling
also closes the upstream socket, which is where the wall clock is actually saved.

**A third thing was wrong and only the real client found it: the minted frames were UNREADABLE on a
cut that landed inside an SSE event.** SSE events are separated by a BLANK LINE, and the proxy
forwards line by line, so a stream that stops between a `data:` line and its terminator leaves the
event OPEN. Everything minted afterwards is then glued in as a second `data:` line of the same
event, a conformant parser concatenates the two payloads, and `json.loads` reports `Extra data:
line 2 column 1`. Driving arm A's own litellm 1.97.0 at the pre-fix proxy in front of an upstream
that dies mid-event: **`MidStreamFallbackError` wrapping an `APIConnectionError`** — one of the
three types AlgoTuner's ten-attempt loop catches. Same upstream, patched proxy: no exception, 32
chunks, the usage read correctly. The frames 2afb287c added were right and could not be read, which
on the wire is the same as absent. The proxy now closes the event before minting into it, and
`_sse_events` in the test splits on the blank line rather than filtering `data:` lines, because a
line filter is blind to exactly this and passed the broken version.

**Measured against arm A's own litellm 1.97.0, and the one thing this does NOT explain.** With the
patch, that litellm raises nothing on any cut shape that could be constructed — streaming, streaming
with `include_usage`, and adapted; 40, 500 and 220,000 deltas; content-only and reasoning-only;
upstream ending cleanly, on a bare FIN, mid-event, and cut by the ceiling — and reports the
synthetic usage and cost correctly in every case. What is still **not reproduced** is arm A's
production exception itself (`APIError: OpenAIException - `, empty message, 3–33 s AFTER the meter
row was written). Three facts bound it. The live proxy (PID 2450834, started 2026-08-24 10:11) runs
the pre-2afb287c code and 0 of the 134 rows carry the new prompt-token basis, so every recorded
abort predates even the two-frame fix. The mid-event failure above is reproduced only on the
STREAMING transport, and arm A is adapted, where partial lines are dropped silently. And 31 of the
134 aborts carry a `BrokenPipeError` — including 15/15 on `integer_factorization/a3` and 13/13
across `kcenters/a3,a4`, three of the retry-storm task-arms — which means the client had already
gone; those are the 600 s client timeout that `config.yaml` raised to 1900 on 2026-08-25, a
different cause with a fix already in. That leaves the `rbf_interpolation`, `pde_heat1d` and
`sparse_eigenvectors_complex` runs (56 aborts, no broken pipe) unexplained. The residual risk is
stated rather than papered over: if their exception has a cause outside the salvage path, the
ceiling bounds it to one call per 135,000 deltas but does not remove it.

### 9b. `max_tokens` — analysed and REJECTED, and my own proposed value was wrong

I suggested `max_tokens: 16384` on the grounds that arm A's largest legitimate completion was
5 795 tokens. **That number was p99.9, not the maximum.** The real maximum is **132 269** — a
`pde_heat1d` call at 06:37 on 2026-08-26 that ran 1 204 s and returned a proper usage frame — and
arm B's p99 is 25 856 with a maximum of 128 572. The cap I proposed would have truncated real
answers, and far more of arm B's than arm A's.

| cap | aborts cut | runaway saved | complete calls truncated |
|---|---|---|---|
| 2 048 | 127/130 | $6.43 | **1 928 / 8 691** |
| 8 192 | 127/130 | $6.21 | **550 / 8 691** |
| 32 768 | 126/130 | $5.34 | 46 / 8 691 |
| 135 000 | 106/130 | $1.91 | 0 / 8 691 |

Rejected for four reasons. It is **not parity-neutral**: at 8 192 it cuts 7.7 % of arm B's calls
against 0.06 % of arm A's, 37× asymmetric, and `config.yaml` untied `max_tokens: 8192` on
2026-08-22 for exactly that. A cap low enough to matter has a measured false-positive rate; one
high enough to be safe recovers 29 %. `max_tokens` is a generation parameter and injecting it
breaks the proxy's stated contract. And the risk is sharper than I put it: the answer would return
200 + usage + `finish_reason: "length"`, which litellm passes through unmapped, so a client reading
only `content` acts on a plausible half-answer where today it raises.

**The alternative worth having** is a proxy-side DELTA CEILING: stop forwarding after N deltas and
emit the same cut-plus-usage frames. It never enters the request, produces the identical observable
the gateway's own 1 800 s cut produces, is symmetric by construction, and at 135 000 has zero
measured false positives in either arm. **Implemented 2026-08-26** — see 9a, which also records why
it went in as the SECOND half of that fix and not the first: on its own it recovers 26 % of the
wall clock, against 69 % for not being retried.

The original finding:

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

### 10a. CLOSED 2026-08-27 — the half of item 10 that is OURS, not the card's

Item 10 shipped the two halves that live in `make_task.py`: the instance shape, and an `eval_train`
command the agent may call. This is the half that lives in the loop, and it was found by looking for
a defect that turned out not to exist.

*The probe corpus is LIVE and still growing — `sol10` went 35 -> 41 plan steps during this analysis — so every count below is the instant it was taken, 2026-08-27, and re-deriving it later will give larger numbers, not different conclusions.*

**The premise that did NOT survive.** "Our evaluation is agent-initiated, so it may never happen."
False at the level that matters. Counted over the eleven model probes in
`/var/tmp/looplab-bench/model-probes/*/runs/*/run/events.jsonl`, `node_created` and `node_evaluated`
are **1:1 in every one of them** — 12/12 on `sol10`, 11/11 on `gpt56luna`, 4/4 on `ctlEdge`; the
three shortfalls (`glm53f` 11/10, `solHull` 3/2, `fxSpectral` 2/1) are runs the spend ceiling cut
mid-evaluation, which is item 5 and not this one. The engine evaluates every node it builds,
unconditionally, and always has. `run_dev_command` is likewise not unused: 76 calls across the
corpus, every one of them `eval_train`.

**What is actually wrong** is one rung lower. A *node* is measured; a *step* is not, and a node
costs ~65 LLM calls (`sol10`: 777 generations, 12 nodes). No session that WRITES code has ever been
shown a number:

* the parent block that carries `metric=` is built in `_run` and consumed only by the
  single-session fallback. On the DEFAULT path (`developer_plan_decompose`) the plan and step
  sessions compose their own user turns and it is dropped: the string `PARENT SOLUTION` appears in
  **0 of 1,055 `plan_step` generations and 0 of 296 `plan` generations** in the corpus;
* the evaluation the engine runs lands after the session that could have used it has ended.

**What the blindness costs, measured.** The model's own answer is to spend a plan step on the
button. Of 116 attributed plan steps (`plan_steps` spans), **30 (26 %) are titled as a measurement
and nothing else** — "Run eval_train and verify speedup", "Measure once with the real evaluator",
"Run the real evaluator on the train split" — and **21 of those 30 wrote no file at all** (`noop`).
Those 30 steps spent **317 LLM calls** (median 7 per step) and **5,762 s**: 30 % of all plan-step
generations and 36 % of all plan-step wall clock, to buy a subprocess that costs 40 s
(`run_dev_command`, n = 76, median 39.6 s, mean 37.0, p90 45.8).

**Fixed.** The engine runs the operator-pinned command BETWEEN plan steps and hands its output to
the next step, and the writing sessions are told what the parent measured. Three properties are
load-bearing and each has a case that dies without it:

1. it runs OUTSIDE `run_phase`, so it spends none of `developer_session_time_budget_s` (1200 s);
   the step sessions it sits between are median 58.9 s / p90 296.3 s and are not squeezed;
2. it runs only after a step that CHANGED a file, and never after the LAST step, whose only reader
   would be the engine's own evaluation. Priced per run over the eight probes that ran a multi-step
   plan (non-final steps x 40 s, against each run's own wall clock, and against the wall clock its
   measurement-only steps already spend):

   | run | wall | plan steps | non-final | added | added % | meas-only steps | their wall | net |
   |---|---|---|---|---|---|---|---|---|
   | `ctlEdge` | 15,276 s | 9 | 5 | 200 s | 1.3 % | 4 | 280 s | **-80 s** |
   | `fxKcenters` | 11,734 s | 8 | 5 | 200 s | 1.7 % | 4 | 1,151 s | **-951 s** |
   | `fxSpectral` | 8,350 s | 4 | 3 | 120 s | 1.4 % | 2 | 1,417 s | **-1,297 s** |
   | `glm53f` | 39,514 s | 26 | 16 | 640 s | 1.6 % | 10 | 1,709 s | **-1,069 s** |
   | `gpt56luna` | 11,506 s | 21 | 12 | 480 s | 4.2 % | 1 | 157 s | +323 s |
   | `sol1` | 1,028 s | 4 | 2 | 80 s | 7.8 % | 1 | 59 s | +21 s |
   | `sol10` | 8,601 s | 41 | 27 | 1,080 s | 12.6 % | 7 | 883 s | +197 s |
   | `solHull` | 2,457 s | 18 | 12 | 480 s | 19.5 % | 4 | 425 s | +55 s |
   | **total** | 98,466 s | | | **3,280 s** | **3.3 %** | | **6,081 s** | **-2,801 s** |

   Stated honestly in both directions: the GROSS add is 3.3 % of the corpus's wall clock but reaches
   19.5 % on the shortest run with the longest plan, and it is only net-negative if the
   measurement-only steps stop being planned — which is why the plan prompt now says the measurement
   is free, and which is the first thing to check on the next arm rather than assumed here;
3. it is a PROMPT INPUT AND NOTHING ELSE. It goes through `DevCommandTools`, which runs in a
   disposable candidate tree it deletes on return, so it cannot write a node file, cannot become
   `last_files`, and cannot reach `node_evaluated.metric`. The reported speedup and the champion
   still come from `engine/evaluate.py` alone.

**OFF by default** (`Settings.developer_step_feedback_command`, empty = off), for item 10's own
reason: it changes WHAT THE AGENT IS SHOWN, which is the measurement, and the twenty arm-B numbers
on disk were produced without it. The command is named, never guessed — nothing in a task marks
which pinned command is the scorer, and a heuristic would run the wrong one.

**Not adopted: a train SUBSAMPLE to make it cheaper.** The table above already nets negative on
five of eight runs, and 3.3 % of corpus wall clock is not what is scarce here — the budget that
binds these runs is SPEND, and the command costs no tokens. So the only thing a subsample would buy
is a second, weaker number in circulation next to the real one — and the rule that keeps it out of the reported speedup
would have to be maintained forever at every site that reads a metric. The cheap number is the same
number here: the full train evaluation, at the price it actually costs.

Guarded by `tests/test_developer_step_feedback.py`, eight cases, seven mutations tried and every one
of them red.


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
