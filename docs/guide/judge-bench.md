# Judge bench — measuring a prompt change instead of believing it

Every LLM judge in this engine is a prompt someone edited on faith. The training-log monitor's kill
authority was changed twice in one week and the only evidence either change helped was that the next
run felt better. This page is about the thing that ends that: a **frozen corpus of recorded
decisions with outcome labels**, and a scorer that replays a candidate over it.

**There are two benches now**, one verb pair each of `python -m looplab.judgebench`, over different
rows and different judges. Everything down to *What this corpus cannot answer* is the first — the
**training-log monitor** (`score`, 450 decisions). *The second bench*, at the bottom of this page,
is the **failure classifier** one layer down (`score-triage`, 122 rows). The rules that hold for
both — a label may never rest on the judge's own answer, agreement is churn and not accuracy, and
the scope caveat travels inside the dataset header — are argued once, in the first bench's sections.

!!! warning "What a number from the monitor corpus is evidence for"

    Seven preserved runs, **one task family** (ESCI dense retrieval, two backbones), **one
    operator**, **one judge model** (`deepseek-v4-flash` in 450 of 450 recorded decisions). A
    prompt optimised against this corpus will overfit it. A number measured here is evidence about
    **this deployment on this task** — never quote it as a general claim about the judge, the
    prompt, or the model. The paragraph is stored in the dataset header and printed above every
    report, so it travels with the number rather than living only on this page.

## The two numbers, and why they must never be merged

| question | what it measures | when it exists |
|---|---|---|
| **agreement with the LABEL** | did the candidate get it **right** | only where the run itself later supplied an outcome the judge did not author |
| **agreement with the RECORDED VERDICT** | how much behaviour the change **moves** | always |

The second is not accuracy. It is a churn measure, and it is *maximised by a candidate that
reproduces every one of the incumbent's mistakes*. `ScoreReport` keeps them in separate fields and
there is deliberately no combined score and no code path that averages them.

## Which judges this can label, and which it cannot

**Outcome-labelled — a correctness claim is possible.**

- **the training-log monitor** — built, 450 decisions. It called a node `broken` or `healthy`, and
  the node then either produced a usable metric or did not.
- **failure triage** (`engine/triage.py`) — **built**; it is the second bench on this page. It named
  a cause and chose repair-or-abandon, and what followed says whether the repair landed. Count
  decisions, never spans: 1,634 recorded `triage` generation spans are only ~**104 decisions**,
  because triage is agentic and one decision is a ~16-turn tool loop — the same collapse takes the
  monitor's 3,950 spans down to 450. The bench in the end counted neither, because the unit that can
  carry a label is the CLASSIFICATION and not the agent's session: **122 rows**, enumerated from the
  durable event log, which also reaches the runs whose spans were pruned.
- **the repair critic** (`engine/repair_verify.py`) — it stopped or continued a chain whose outcome
  is recorded, but there are **7 decisions in the whole corpus**. Too few to bench; a bench built on
  it would report noise with a decimal point.

**Unlabelled — a correctness claim is impossible, now and permanently.**

- **the novelty gate.** A rejected idea is never run, so nothing on disk says whether it would have
  worked, and nothing ever will. It can be scored only for **consistency** with the old verdict, and
  "agrees with the old model" is a score maximised by reproducing the old model's mistakes.
  `label_coverage: 0` is how the scorer reports that, and `label_accuracy` returns `None` rather
  than `0.0` — "no evidence" and "always wrong" are different answers and a chart cannot tell them
  apart.

## The label

One row per **decision** (a whole agentic tool loop, not one LLM call). The label comes from the
eval attempt the decision was watching:

| status of the watched stage in that attempt | label |
|---|---|
| `fail` / `expect_failed` / `check_failed` | `wasted` |
| `ok` / `reused`, node later scored ≤ 5 % of the run's best | `wasted` |
| `ok` / `reused`, node later scored a usable metric | `productive` |
| `timeout` | `budget_exhausted` — **excluded from accuracy** |
| no attempt found, node never evaluated, direction is `min` | `unknown` |

Three choices worth arguing with:

- **Stage status alone is not the label.** `e5small-dr-unified-v2` node 2 trained for 6.4 hours,
  the stage exited `ok`, and the model it produced scored `0.0`. That is precisely the case this
  judge exists for, and a stage-status label would score it `productive`.
- **`timeout` is its own class.** The compute was wasted, but the monitor's own system prompt tells
  it that a run which is "merely slow or plateauing but still progressing is `watch`, not `broken`".
  Charging those as missed stops penalises the judge for obeying its instructions. It moves 60 of
  450 decisions and it would change the headline.
- **A stage that worked is not charged for a later stage failing.** Eight decisions watched `mine`
  on nodes whose `train` crashed minutes later. `mine` was fine; the judge watching it could not see
  `train`. Those are `productive`.

## What the incumbent scores

Replaying the recorded verdicts against the labels — `python -m looplab.judgebench score`:

| | all 450 | with log tools (369) | without (81) |
|---|---|---|---|
| outcome-labelled decisions | 354 | 285 | 69 |
| accuracy | **0.701** | **0.782** | **0.362** |
| said `broken`, run finished fine | **5** | **1** | **4** |
| never said `broken` on a wasted run | 101 | 61 | 40 |

Two findings and one warning:

- **On the case it exists for, it is right.** Of the 53 decisions watching a training that completed
  and produced a dead model (`0.0` and `2e-05`), **48 called it `broken`**, 4 `healthy`, 1 `watch`.
  Both attempts were caught, the first `broken` arriving with 6.5 h and 4.7 h of the EVAL still
  to run — train plus score, since the saveable figure is measured to the attempt's flush
  instant and not to the end of training alone.
- **False alarms are rare and concentrated.** 5 of 450 decisions called `broken` on a run that
  finished fine — and **4 of the 5 are the two runs where the judge had no log tools**, the
  flat-tail misreading `train_monitor.py`'s trajectory section already documents.
- **That last comparison was a confound, and it is now SETTLED — the tools are not what makes the
  difference.** See the next section.
- **101 "missed stops" is not 101 mistakes.** Most are early looks at an attempt that failed later,
  and 38 of them watched a stage the engine failed *after* it exited, over artifacts the judged log
  never showed. Cut them with `--exclude-basis`, or read the per-attempt view, which asks the
  question an operator actually pays for: **7 of 27 wasted attempts caught, 3 of 49 productive
  attempts falsely stopped, ≈18.9 h of compute saveable.**

## The live arm, run — and the tools are not what made the difference

`score.llm_candidate` had never been run. It has now: **450 provider calls, one per row, 0 errors,
799 s wall clock**, `deepseek-v4-flash` at the runs' own `llm_temperature` of 0.6, against the
operator's own endpoint, ~603 k prompt tokens in total (2.4 MB of stored prompt text). Concurrency
was held at 2 because a live run shares that endpoint; the throttling it drew is what the 799 s is. The arm replays each row's recorded prompt with the tool affordance
REMOVED — `look_invitation` overridden to empty, which is byte-for-byte what the engine splices
when `train_monitor_tools` is off — so the 369 rows the incumbent answered WITH tools are answered
again, same model, same evidence, tools gone. That is the same-rows A/B the slice table could not be.

| the 285 outcome-labelled rows the incumbent had TOOLS for | accuracy | false stop | true stop |
|---|---|---|---|
| **A** recorded — with tools (agentic, median 3 spans/decision) | **0.782** | **1** | **51** |
| **B** live replay — SAME rows, tool affordance removed | **0.765** | **1** | **46** |

| the 81 rows nobody ever had tools for — the FIDELITY control | accuracy | false stop | true stop |
|---|---|---|---|
| recorded | 0.362 | 4 | 2 |
| **C** live replay, same conditions | 0.391 | 4 | 4 |

Read them together:

- **Taking the tools away costs 0.017 accuracy and 5 of 51 true stops, and adds no false stop.**
  Not nothing — five true stops is five — but it is nowhere near the 0.420 the slice table
  suggested.
- **Arm C is what licenses that comparison.** Replaying rows under conditions identical to how they
  were recorded reproduces the error rates (0.362 → 0.391, 4 → 4 false stops) while moving 41 % of
  the individual answers (churn 0.593 at temperature 0.6). So the model is noisy per decision and
  stable in aggregate, and a 0.017 gap in arm B is inside that noise while a 0.420 one is not.
- **So the 0.362 is telling us about the RUNS.** The failure mixes are disjoint where it counts:

  | | wasted / `node_metric_degenerate` | wasted / `stage_failed` | productive |
  |---|---|---|---|
  | with tools (v2/v3/v8/v9) | 53 rows, **48 caught** | 59 rows, 3 caught | 173 rows, 1 false stop |
  | without tools (v6/v7) | **0 rows** | 42 rows, 2 caught | 27 rows, 4 false stops |

  v6/v7 contain **not one** decision of the class this judge exists for — a training that runs to
  completion and produces a dead model — and 100 % of their wasted population is `stage_failed`,
  the class caught at 5 % *with* tools and 4.8 % without. Standardise the with-tools slice to the
  without-tools basis mix and it scores **0.420**, against 0.362 observed. The reverse
  standardisation is not computable at all: two of the four cells are empty on the v6/v7 side, which
  is the honest statement — **the corpus cannot identify a tools effect on accuracy, and the live
  arm says the effect it can measure is small.**

The residual after mix — the false-stop rate, 0.6 % of productive decisions with tools against
14.8 % without — survives arm B unchanged (1 of 173 and 4 of 27 in BOTH arms), so it is not the
tools either. It tracks the measured TRAJECTORY instead: **0 of the 107 productive decisions that
carried a `trajectory` block were called `broken`**, and all four v6 false stops are the flat-tail
misread the trajectory section of `engine/train_monitor.py` was written for. That is suggestive and
it is **not** measurable here: the tracker postdates v6/v7, so there is no stored measurement to
splice into their prompts and no arm that could add one.

## The verdict is not the stop (`--gate`)

The bench's headline counts a stop as `status == "broken"`. The engine does not act on a verdict —
it acts on `should_monitor_kill` / `should_monitor_repair`, a conjunction the model supplies two
terms of. Applying the conjuncts a recorded row can still answer:

| `python -m looplab.judgebench score …` | accuracy | false stop | true stop | attempts caught | attempts falsely stopped |
|---|---|---|---|---|---|
| (default — the verdict) | 0.701 | **5** | 53 | 7 of 27 | 3 of 49 |
| `--gate` (the engine's own bar) | 0.703 | **0** | 49 | 6 of 27 | **0 of 49** |

**All five false stops sit at confidence 0.62–0.75, below the shipped
`train_monitor_kill_confidence` of 0.8; 49 of the 53 true stops clear it.** The confidence field is
the single most discriminating signal in this corpus and the gate already spends it. A prompt
change benched against "5 false stops" is being scored against a number the engine never pays —
in the expensive direction, because it invites loosening something that is not loose.

What `--gate` deliberately does **not** model:

- **the confirm streak.** `_MONITOR_KILL_CONFIRM_TICKS` wants two consecutive `broken` verdicts, and
  the corpus has no confirmation look in it at all: the arm that schedules one at
  `_MONITOR_CONFIRM_DELAY_S` (30 s) was gated on `_KILL_ELIGIBLE_ROLES` and every recorded alert
  carries `log_role: work`. Measured, the median gap from a `broken` verdict to the next decision
  about the same attempt is **617.8 s** (min 605.5, n = 76) — the ordinary cadence. Simulating a
  streak over ticks ten minutes apart reports 2 of 27 attempts caught instead of 7, which is a fact
  about the corpus's spacing and not about the gate.
- **the role conjunct**, for the same reason it is why nothing in this corpus was ever stopped: all
  450 alerts are `log_role: work`, so the *kill* count here is 0 of 0.

The measured-trajectory veto IS modelled, from each row's own stored `trajectory` text, and it is
**inert**: 144 of 144 rows with a `descending` measured curve were judged `healthy`, so it never
meets a `broken` to refuse. `tests/test_judge_bench.py` pins that rather than assuming it.

## What the 101 missed stops are actually made of

At the unit compute is paid in — the eval attempt — every one of the **20 uncaught wasted attempts
is `stage_failed`**. Not one is a training that completed and produced a dead model; both of those
were caught, at 31 of 33 and 17 of 20 decisions. Splitting the 20.1 h an ORACLE could have saved by
stopping each uncaught attempt at its first look:

| how the stage ended | uncaught attempts | oracle-saveable |
|---|---|---|
| `check_failed` / `expect_failed` — the stage exited **rc 0** and the ENGINE then failed it, over artifacts the judged log never showed | 7 | **13.4 h** |
| `fail` — a real crash, 5 of them under six minutes after the look that is charged for missing it | 13 | 6.6 h |

**Two-thirds of the money in this error class is in a class the judge is architecturally blind to.**
No prompt change reaches it. What would is evidence: the declared `expect`/`assert` contract the
engine is about to check, put in front of the judge while the stage is still running.

Two things the **second bench on this page** adds to that, and they are the reason these two
sections are one page:

- **A `check_failed` is not automatically a failure.** *What the corpus refuted*, below, shows the
  stage check calling ten converged trainings "no learning progress" off the last 4,000 characters
  of their logs — nodes the operator then reset, whose `train` came back `reused` (the very
  checkpoint the check condemned) and which scored 0.805–0.8662 against a run best of 0.8835. So
  the 7 `check_failed`/`expect_failed` attempts above are charged to the monitor against a verdict
  that was itself wrong ten times elsewhere in the corpus. It does not rescue the monitor — those
  attempts *were* failed by the engine and the compute *was* discarded — but "the judge could not
  see the evidence" and "the evidence was misread by a different rule" are the same
  disease at two layers, and widening what the monitor is shown is the fix for both. **That second
  layer is now fixed** (`engine/eval_stages.py`, 2026-08-20): `loss_unchanged_from_first_step` is
  measured over the whole of the attempt's stage log instead of a 4,000-character window that
  structurally cannot contain the first step. So an unknown share of the 13.4 h below is a checker
  that was wrong rather than a monitor that was blind, and **that share is not re-measured here** —
  the corpus is frozen at decisions taken before the fix, and re-labelling it against a rule that
  did not exist when the runs ran is exactly the retro-fit `judge_corpus.py` refuses elsewhere.
- **The record does not preserve what the decider saw.** The frozen `_is_torch_oom` — the rule as
  it stood when that corpus was cut, since deleted from production — scores **0 of 23** over the
  durable stderr tail, because not one of the 122 preserved tails carries a marker at all: the live
  classifier read a 64 KB clamp and the event log kept ~500 characters. That is exactly the shape of
  the 13.4 h above: an error class neither bench can measure past, because what was decided over is
  gone. Both benches state the limit in their dataset header rather than scoring around it, and both
  reach it the same way — by widening what the decider is SHOWN, not by rewording what it is asked.

## `fault` — unmeasurable from the record, and NOT unmeasurable

`TrainingVerdict.fault` decides whether a stop is a REPAIR (`implementation`) or a terminal
(`hypothesis`). `recorded.fault` is `None` in 450 of 450 rows because the field postdates every
preserved run — the extractor already reads it, so the corpus repairs itself the moment a run
records one. But the record is not the only place to look. Two arms, over the **88 rows where
either the incumbent or the tool-less replay said `broken`**, i.e. the whole population where a
repair-stop is on the table:

| the same 88 rows | `broken` | of those: `implementation` | `hypothesis` | `unknown` | `environment` |
|---|---|---|---|---|---|
| live replay, no tools | 79 | **51** (65 %) | 19 | 6 | 3 |
| live replay, `monitor_code_tools` over the preserved workdir | 69 | **35** (51 %) | 22 | 5 | 7 |

**The finding is the instability, not either column.** Of the 67 rows *both* arms call `broken`,
letting the judge read the code moves `fault` on **34 — half of them** — including 14
`implementation` → `hypothesis` and 8 the other way. Downstream that halves the repair-stop:
applying `--gate`'s conjuncts plus `fault == "implementation"`, the tool-less arm would repair-stop
**39** decisions and the code-reading arm **19**. So the volume of repair-stops a run pays for is a
function of what the judge was allowed to see, and the affordance shipped *specifically* to make
this field reliable is the thing that moves half its answers.

Two things it does say, and they are the ones worth acting on:

- **`fault` does not default to the safe answer.** The schema tells the model `unknown` is safe; it
  answers `unknown` 6 times in 79 without the code and 5 in 69 with it. It commits.
- **On a run that finished fine, it commits the wrong way.** 5 of the productive decisions drew
  `broken` and 4 of those said `implementation` — but every one is below the confidence bar, so
  through the gate the repair-stop's own false-stop rate is **1 decision, 1 of 49 productive
  attempts**, in both arms. The gate is again what holds the line.

**Two honest limits on the code arm.** The workdir on disk is the FINAL state after every repair,
so for a repaired node the judge read code that is newer than the log it was judging — which biases
it toward a coherent story. And neither arm is agentic in the way the incumbent was for those rows.

**What would make `fault` properly benchable** is a label, and it is not the one this dataset has.
`wasted`/`productive` says whether stopping was right; `fault` asks whether the REPAIR was the right
remedy, and the outcome that answers it is what the repair then did — `node_repaired` plus the next
attempt's terminal, which is the same join the (unbuilt) triage bench needs. A run must also
actually reach the branch: `train_monitor_kill` on, a `broken` at ≥ 0.8 confirmed twice, and
`fault="implementation"`. No preserved run did, because the confirmation arm never fired on a
`work` stage — which is fixed, and is the other half of this change.

## Using it

```bash
python -m looplab.judgebench extract runs/*                 # rebuild the dataset from runs/
python -m looplab.judgebench score                          # the incumbent replaying itself
python -m looplab.judgebench score --answers candidate.jsonl # a candidate's captured answers, offline
python -m looplab.judgebench score --stage train --tools with
python -m looplab.judgebench score --exclude-basis stage_failed
python -m looplab.judgebench score --gate                    # count the ENGINE's stop, not the verdict
```

`score` makes **no network call**. A candidate is supplied as a JSONL of `{"case_id", "status"}`.

To bench a *changed prompt* rather than a changed model, every row stores the prompt split back into
the ingredients the engine assembled it from (`system`, `context`, `stage_context`, `trajectory`,
`look_invitation`, `digest`), verified by re-joining byte for byte — all 450 rows split exactly. Swap
one ingredient, re-render with `render_prompt`, and the candidate answers over the *same recorded
evidence*. `score.llm_candidate` is that arm; **constructing it is the decision to spend money**, one
provider call per row, and it is not the default.

## The dataset

`tests/data/judge_bench/train_monitor.v1.jsonl.gz` — 450 rows, **278 KB** gzipped from 4.8 MB raw.

It is **committed** because `runs/` is not in the repository: an on-demand dataset cannot be read by
a reviewer judging a prompt change, cannot gate a merge, and cannot be diffed. It is **derived** and
never hand-edited, and that claim rests on three guards in `tests/test_judge_bench.py`, in
decreasing strength:

1. `test_the_dataset_regenerates_from_the_runs_it_names` rebuilds it byte for byte — but it needs
   the source runs, so point `LOOPLAB_BENCH_RUNS` at them. **It SKIPS otherwise, and until
   2026-08-20 that skip was the whole derivation story: `runs/` is gitignored and the variable was
   set nowhere, so the test had never once executed.** A skip unreachable in CI is not a guard.
2. `test_every_label_rederives` recomputes every LABEL from the row's own stored facts through the
   production rule, offline, so an edited label goes red on a machine with no corpus — and
   `test_the_committed_corpus_is_internally_derived` does the same for every field the label rule
   does NOT read, by re-deriving the JOINS the extractor built each row from: `case_id` from its
   provenance, `system_prompt_sha256` from the row's own stored `system`, `llm_calls` from the
   length of `span_ids`, `tools_available` from `bool(look_invitation)`, the sort order, and the
   header's per-run counts. A hand edit has to forge all of them consistently, and editing a stored
   PROMPT — the field the replay seam is entirely about, which no label and no headline number
   reads — is caught here and nowhere else.
3. `test_the_committed_artifact_has_not_been_edited_in_place` pins the artefact's uncompressed
   sha256. A tripwire and nothing more: an editor can update the constant. What it buys is that the
   edit cannot be *invisible* in a diff.

The **replay seam** has its own guard, and it was vacuous for the same reason: until 2026-08-20
`test_prompt_splits_round_trip_exactly` asserted `render_prompt(row["prompt"]) == messages_of(row)`,
and `messages_of` *is* `render_prompt(row["prompt"])` for a row with no stored `messages` — which is
all 450. It now round-trips each row's ingredients back through the production splitter, checks that
`prompt_split_exact` is capable of being false, and pins `render_prompt` against what
`_training_verdict` actually sends.

Every stored text goes through `core/redact.py::redact_output_tail` — the same screen persisted
output tails already pass, entropy pass included, not a second rule. Two honest limits:
`redact_env_values` screens the environment of the process running the *extractor*, which need not
be the environment the run had; and the split's byte-for-byte check is performed on the redacted
text.

## What this corpus cannot answer

- **Model choice.** One model produced all 450 verdicts.
- **`TrainingVerdict.fault`, from the RECORD.** Not one recorded verdict carries it; the field
  postdates every preserved run, and `recorded.fault` is `None` in 450 of 450 rows. **It is not
  unmeasurable, though — it is unmeasurABLE FROM THE RECORD, which is a different sentence**, and
  the live arm closes most of the gap today. See the section below.
- **Whether a `broken` was worth acting on.** No run in the corpus ever exercised the kill path —
  every alert carries `log_role: work`, which has no kill authority — so the corpus records what the
  judge *said*, never what a kill would have cost.

## The second bench — the failure classifier

`engine/triage.py::_failure_reason` answers the question one layer below the monitor's: **this eval
ended with no usable metric — why?** Its answer is not a report. It selects the repair directive
(`crash_repair._repair_error_context`), it gates the triage-driven dependency install
(`_prepare_env_from_triage`, which runs for `crash` and nothing else), and it is read by the salvage
gate (`metric_salvage.NEVER_SALVAGED_REASONS`). An agent-driven diagnostician is replacing the
substring rules that produce that answer, and without a corpus the only evidence the swap helped
would be that the next run felt better.

!!! warning "What a number from the failure-classifier corpus is evidence for"

    Seven preserved runs, **one task family** (ESCI dense retrieval, two backbones), **one
    operator**, one box. Same caveat as the monitor corpus and for the same reason: a rule tuned
    against these 122 rows will overfit them. `CORPUS_LIMITS` travels in the dataset header and is
    printed above every report, so the caveat cannot be separated from the number.

### The row is a classification, and it is counted from the log

One row is **one failure-classification event**: one eval attempt that ended with no metric, and
therefore exactly one call of `_failure_reason`. Rows are enumerated from the durable event log —
every `node_repaired` (the attempt before the repair failed, and was classified) and every
`node_failed` whose reason is a real reading of an eval — and **not from spans**, so a run whose
spans were pruned still contributes. A terminal that follows its own repair with no intervening
eval is the same failure and is merged into it; the merge test is deliberately not "identical error
text", because `e5small-dr-unified-v3` node 2 failed its artifact contract four times with the same
message to the byte and text-merging would have collapsed four distinct classifications into one.

122 rows, from the 7 preserved runs that hold a failure at all — `e5small-dr-unified-v2`/`-v3`,
`rubertlite-dense-retrieval`, `rubertlite-dr-unified-v6`/`-v7`/`-v8`/`-v9`. **118 carry a real
label; the other 4 are `unknown` and are excluded from the matrix rather than guessed** — a padded
corpus is worse than a thin one, and the `unknown` count is printed with every report. 115 of the
118 rest on a high-confidence basis.

An eighth run in `runs/` — `e5small-dr-unified-v4` — is **refused**, and the refusal is about the
label rather than about tidiness: every label rests on what happened NEXT (the repair that followed,
the reset that reused the condemned stage, the metric the artefact eventually scored), and a run
still in flight has not produced its own next. It is not hypothetical. While this corpus was being
built, `-v4` grew from 292 to 853 events between two extractions minutes apart and silently added
one unlabellable row to the second, so the artefact would have stopped being reproducible with
nothing saying why. `extract_run` now raises `LiveRunRefused` for any run whose event log was
appended to within `LIVE_RUN_GRACE_S` (1 h), the header lists what it skipped, and each source
records the `last_seq` it was read at so a rebuild over a grown run is a visible header diff.
`run_finished` is deliberately not the test — three of the seven preserved runs never wrote one.

### The label, and the closed list of what it may rest on

**The rule's own answer cannot be the label** — that measures agreement, not accuracy. Every label
rests on a fact `_failure_reason` did not author, and `LABEL_BASES` is the closed list of what those
facts may be. The last column is the historical incumbent's accuracy *on the rows that basis
labelled*, which is where the aggregate number comes apart:

| basis | the fact it rests on | confidence | incumbent |
|---|---|---|---|
| `reused_stage_later_scored` | the operator reset the node, the SAME stage output came back `reused`, and it scored a healthy metric — so the stage did not fail | high | 0/10 |
| `oom_marker_in_evidence` | a `_TORCH_OOM_MARKERS` string in the log the triage agent read, or in the paired stage log | high | 0/16 |
| `allocator_message_in_stderr` | the allocator's own message body (`Tried to allocate … GiB … free`) in the recorded tail | high | 0/7 |
| `watchdog_sentinel` | the ENGINE's own `‼ LOOPLAB health-check:` line, which only the engine writes | high | 5/8 |
| `stage_timeout` | `stage_finished.status == "timeout"` — the engine's own clock | high | 6/6 |
| `nonfinite_loss_in_log` | repeated `loss=nan` / `loss=inf` / `loss=-2e+10` in the paired stage log | high | 0/3 |
| `artifact_contract` | `expect_failed`: the engine compared the declared path's mtime to the stage start | high | 8/9 |
| `declared_condition_violated` | the check quotes the epoch the log reports against the epoch the manifest declared | high | 5/5 |
| `terminal_exception` | a named non-OOM Python exception on the last line of the traceback | high | 50/50 |
| `logged_fatal_error` | the program's own logger printed the fatal condition and then exited non-zero | high | 1/1 |
| `check_concern_nonfinite` | the stage checker quotes a non-finite or exploded loss it read out of the log | medium | 0/1 |
| `check_concern_no_learning` | the stage checker called the training dead, with no reuse-score to acquit it and nothing non-finite to convict the numerics | medium | 0/1 |
| `reviewed` | a case the rules cannot reach, read by hand, carrying the exact evidence string it was read from | medium | 1/1 |

Read that column before the headline: the incumbent is **50 of 50** where a traceback names the
exception, and **0 of 36** across the bases whose evidence is memory pressure (23 rows), a stage
that never failed (10) or a blown objective (3).

**No label rests on a model's prose, and a rule was deleted to keep it that way.** The obvious extra
basis is the triage agent's own rationale — it had the log tools, it read the stage log, and it
repeatedly contradicts the reason it was handed. That rule was written, run and removed: it fired on
**exactly 1 row in 122, and on that row it was wrong**, because the rationale recited a failure
*history* ("OOM → faiss GPU error → no-traceback crash") and a substring rule read the history as
the diagnosis. Every other OOM it would have caught was already caught by the allocator's own words,
so the rule bought nothing and the row it got wrong is now `unknown`. All the `oom` labels rest on
strings torch itself printed — 16 on a marker, 7 on the allocator's message body.

### Three arms, never averaged

| arm | accuracy | agreement with the recorded reason |
|---|---|---|
| `recorded` — the incumbent as it actually ran, zero reconstruction | **76/118 = 64.4 %** | 100 % by construction |
| `--arm frozen` — the 2026-08-20 classifier, over the durable ~500-char stderr tail | **88/118 = 74.6 %** | 82.9 % |
| `--arm frozen-widened` — the same rule, plus the triage agent's own log reads | **104/118 = 88.1 %** | 69.2 % |
| `--arm live` — `_failure_reason` at HEAD (the deterministic half, whatever ships) | **88/118 = 74.6 %** | 82.9 % |

**The two kinds of reference to production, and why they are treated differently.** `frozen` reads
no production name at all: `triage_score._frozen_failure_reason_v1` is a verbatim snapshot of the
classifier as it stood when this corpus was cut, and `HISTORICAL_AUTHENTICATED_REASONS` and
`TORCH_OOM_MARKERS` are frozen beside it (and copied into the dataset header, so they travel with
the artefact). That is the correction of a real defect: this arm used to import the live
`_failure_reason`, so when the ownership split landed hours later the arm silently began measuring a
different program while still being labelled "the incumbent". A bench may lose many things; the
record of how the old decider scored is not one of them. `live` is the opposite and must follow
production, because scoring what ships is the point of it — and it **detects** production's
partition rather than importing one by name. Two have shipped inside a week
(`AUTHENTICATED_FAILURE_REASONS` / `JUDGED_FAILURE_REASONS`, and the ownership split's
`ENGINE_FINAL_REASONS` / `DIAGNOSABLE_ENGINE_REASONS`), and an arm that hard-imported either would
be an `ImportError` on the other side of that change — which is how this module went red in the
first place, and is not something a measurement leg may do to a merge. The report prints the
detected shape above the score, because the same number means different things under the two. `--arm head` / `--arm head-widened` are
deprecated aliases that resolve to the frozen arms — which is what they meant when their numbers
were recorded.

The second column is the same churn measure the monitor bench keeps in its own field: it is
maximised by a candidate that reproduces every one of the incumbent's mistakes, and nothing in
`triage_score.py` averages the two.

The recorded reason is kept AS the incumbent's answer and never recomputed for the primary matrix,
because the code each run was on is what actually chose its repairs. The other two arms exist to
separate **the rule from the window** — and on this corpus that is not a nicety.

### The half that matters: what a diagnostician actually replaces

`_failure_reason` answers two different kinds of question, and `triage.py`'s own fact/reading split
names them. On an **authenticated** truth (`timeout`, `diverged`, `stalled`, `expect_failed`,
`needs_failed`, `check_failed`, `drift`, `setup`) the engine observed the failure out of band, and
this bench's label and the classifier frequently read the SAME engine fact — `check_failed` comes
from `res.stages[-1]["status"]` on both sides. On the other half (`crash` / `oom` / `no_metric`,
plus the `not_learning` only a live judge can name) the engine is inferring a cause from what the
dead process wrote. **That second half is the whole of what an agentic diagnostician replaces**, so
the report prints it separately and it is the number to judge a candidate on:

| arm | authenticated | read from the text |
|---|---|---|
| `recorded` | 24/42 = 57.1 % | **52/76 = 68.4 %** |
| `--arm frozen` (= `--arm live`, identical on every row) | 38/42 = 90.5 % | **50/76 = 65.8 %** |
| `--arm frozen-widened` | 38/42 = 90.5 % | **66/76 = 86.8 %** |

Read the middle row before quoting the headline. The +10.2 points overall is almost entirely the
authenticated half — the `check_failed` branch existing, which is a real 2026-08 fix and is how the
ten `no_metric` terminals below got their right answer. **On the text-read half both the frozen and the live classifier are 2 rows WORSE
than the one that actually ran**, and it only pulls ahead when it is handed evidence the
durable record does not contain. A single accuracy number hides that completely, which is the same
reason this bench refuses a weighted cost total.

### Did replacing the regexes help? The first real answer

The classifier is being split by OWNERSHIP: the engine answers only what it caused, ran or measured,
and `crash` / `no_metric` / `check_failed` become nominations handed to a diagnostician. Scored on
these 118 labelled rows — with the split merged on top of this corpus — **the deterministic half of
the new classifier answers exactly what the old one did: 88/118 = 74.6 %, and identical answers on
all 122 rows.** Not one row moved.

That is not a disappointment; it is the measurement the ownership argument never had, and it says
precisely where the change's value has to come from. The two rules that were deleted (`_is_torch_oom`
and the `-9/137`-with-no-traceback kernel signature) decided **nothing** on this corpus: the marker
rule already scored 0 of 23 over the durable record, and the kernel signature was shadowed on every
row by a watchdog branch above it. Deleting them cost no accuracy and removed two text rules — the
right trade on principle, with the accuracy column now on the record rather than assumed.

**All of the upside is in the handoff, and it is large.** The live report prints it and refuses to
fold it into the accuracy line:

```
HANDED TO THE DIAGNOSTICIAN  (a nomination, not a decision)
  94 of 118 labelled rows got an answer in ['check_failed', 'crash', 'no_metric']
  65 of those 94 happen to be right already; the other 29 are the headroom a diagnostician
  has to win, and are what `--answers` scores. This is NOT part of any accuracy claim above.
```

Those 29 rows are 22 `oom`, 4 `diverged`, and one each of `not_learning`, `crash` and `no_metric`.
Getting none of them leaves the corpus at 74.6 %; getting every one it is *allowed* to get takes it
to **113/118 = 95.8 %**, not to 99.2 %, and the gap is worth stating plainly:

**4 of the 29 have a ground truth the diagnostician may not say.** Their truth is `diverged`, which
is engine-final and absent from `DIAGNOSED_FAILURE_REASONS` — deliberately, because answering it
would be a model asserting that a watchdog it cannot observe fired. All four are
`rubertlite-dense-retrieval` nodes the stage checker caught (`loss=inf` for twenty epochs, `nan`,
`-1.5e+10`, `-2.35e+08`) in a run that predates the diverge watchdog. The rule is right and the
rows are unwinnable under it; the honest ceiling is the one that says so. The best available answer
for them is `not_learning`, which is in the vocabulary and is the wrong cause — "stabilise the
numerics" and "the objective cannot descend" are different directives — so this is a real residual,
not a scoring artefact.

**Nobody has run that arm yet.** `--answers cand.jsonl` takes `{case_id, reason}` and is how it
gets run, offline, over the identical rows.

A caution that comes straight from the numbers below: the diagnostician is being asked to decide
exactly the rows whose durable evidence is thinnest. On the frozen arm, widening the evidence from
the 500-char tail to the triage agent's own log reads moved 16 rows; that is the same evidence a
diagnostician needs, and it is not in the durable record.

### The sharpest finding: the marker rule's win was the window

`_is_torch_oom` (landed and deleted on 2026-08-20) scored **0 of 23** OOMs replayed over the durable stderr tail,
and **16 of 23** once the triage agent's own log reads are in front of it. **Not one of the 122
recorded stderr tails contains any `_TORCH_OOM_MARKERS` string** — what survived to disk is
`node_repaired.error_in`, 500 characters, not the 64,000-byte stream `_failure_reason` actually
read. So the arm labelled "the rule at HEAD" is strictly worse informed than the engine was, and the
gap between the two arms is the value of *looking further*, not of *reading better*.

The 7 it still misses with the wide window are exactly the rows whose capture was truncated **past**
the exception line and kept only the allocator's message body — `Tried to allocate 8.79 GiB. GPU 0
has a total capacity of 139.80 GiB of which 4.59 GiB is free`, a string `_TORCH_OOM_MARKERS` does
not list. That is an actionable finding about the marker list, and the bench is the only thing that
could have produced it.

### What the incumbent gets wrong, and what each error costs

Confusion matrix of `recorded` (truth → answer), 118 labelled rows:

| truth | n | answered |
|---|---|---|
| `crash` | 51 | 51 correct |
| `oom` | 23 | **23 answered `crash`** — cost class `generic_for_specific` |
| `check_failed` | 15 | 10 answered `no_metric`, 5 correct |
| `diverged` | 10 | 3 correct, 4 `no_metric`, **3 `oom`** — cost class `opposed_directive` |
| `expect_failed` | 9 | 8 correct, 1 no answer |
| `timeout` | 6 | 6 correct |
| `stalled` | 2 | 2 correct |
| `no_metric` | 1 | 1 correct |
| `not_learning` | 1 | answered `no_metric` |

Cost totals: `generic_for_specific` 28, `wrong_within_group` 10, `opposed_directive` 3, no answer 1.

At HEAD (`--arm live`, byte-identical to `--arm frozen` on this corpus) the shape moves rather than shrinking: `check_failed` 15/15, `crash` 50/51
(1 → `check_failed`), `diverged` 6/10 (4 → `check_failed`), `expect_failed` 9/9, `timeout` 6/6,
`stalled` 2/2 — and `oom` **0 of 23** (22 → `crash`, 1 unanswerable), `no_metric` 0/1,
`not_learning` 0/1. The `check_failed` column is a later fix working; the `oom` column is the window.

**A single accuracy number hides all of that**, which is why the report prints the cost class in
every off-diagonal cell:

- `admits_refused_metric` — the truth is a reason the salvage gate REFUSES and the answer is not, so
  a metric the trust gate would have rejected can be admitted. **The only error here that can move a
  champion.** Nothing in this corpus does it; it is scored because it is the direction that cannot
  be undone.
- `suppresses_real_metric` — the answer is a `NEVER_SALVAGED` reason and the truth is not: a metric
  the eval really produced is refused, costing the node's whole compute.
- `opposed_directive` — the directive points the *opposite* way. Measured on
  `rubertlite-dr-unified-v6` node 5: three rounds halving 8192 → 2048 → 512 → 256 at ~3 GPU-minutes
  each against an instability the batch size had nothing to do with.
- `generic_for_specific` — the truth has a directive of its own and the answer falls back to
  "diagnose the root cause". Measured on `e5small-dr-unified-v3`: 8 repairs over 3 nodes, 2 of them
  returning byte-identical files, 0 metrics, run stopped systemic. **This is 28 of the incumbent's
  42 errors.**
- `specific_for_generic` — a specific directive issued for a failure with no such shape: one round
  pointed at memory, or at the numerics, instead of at the real bug.
- `misses_dependency_install` — the truth is `crash` and the answer is not, so
  `_prepare_env_from_triage` never runs and a missing library is never installed.
- `wrong_within_group` — both answers reach the same directive and the same gates; the cost is the
  AUDIT TRAIL only, which every later reader and the cross-attempt critic then read.

Two deliberate omissions. `timeout` and `diverged` are in `NEVER_SALVAGED_REASONS` and are **not**
charged as salvage-gate errors: `metric_salvage` re-reads `res.timed_out` / `res.diverged` directly
one line below the reason test, so a wrong label there cannot open the gate, and charging it would
be the bench inventing a cost. And there is **no weighted total** — a weighted total is a knob, and
a knob is how a bench is made to say what its author wanted.

### What the corpus refuted

The bench was built to check a premise: that the 16 terminals in `runs/rubertlite-dense-retrieval`
are really `not_learning` and were classified `check_failed`. **Both halves are wrong**, and the
corpus says so from evidence rather than from a second opinion:

- they were recorded `no_metric` — the `check_failed` reason did not exist yet. Replayed at HEAD
  they *do* come out `check_failed`, which is that later fix working;
- **10 of the 16 were not failures at all.** The operator later reset each node from the `score`
  stage, the train stage came back `reused` (seconds = 0.0 — the very checkpoint the checker
  condemned), and the node scored 0.805–0.8662 against a run best of 0.8835, i.e. 0.91×–0.98× of
  best. The stage check reads only the last 4,000 characters of the log; it saw a flat loss inside
  the final epoch and called a converged training "no learning progress". Node 1's loss went
  33.9 → 13.3 monotonically and it scored 0.805;
- **and that window is since fixed.** The trajectory veto (`engine/eval_stages.py`, 2026-08-20)
  widened what the checker is judged on, so a rerun of those ten today would not be condemned.
  **No label moves** — what acquits them is the operator's own reused-and-scored re-run, not the
  checker's later repair — but "10 of 16 were false refusals" is a property of a checker with a
  broken window, not of the checker that ships, and any rate quoted from these rows *about stage
  checking* is a historical rate. The failure-**classification** scores are unaffected: they replay
  a recorded `res` whose stage statuses were written at the time, so a change to the live checker
  cannot move them — verified, `--arm live` is 88/118 both before and after the veto landed.
  `CORPUS_LIMITS` carries this in the dataset header, so it prints above every report;
- 4 of the 16 are `diverged` (`loss=inf` for all 20 epochs; `loss=nan`; `loss=-1.5e+10`;
  `-2.35e+08`), caught by the stage checker only because the diverge watchdog did not exist yet;
- **exactly one — node 12 — is genuinely `not_learning`**: loss fell 0.986 → 0.0195 monotonically
  while validation recall@100 stayed at 0.0028. That is the case the word was added for, and it is
  1 row in 122, not 16;
- the last, node 40, is a `soup` stage that exited 0 having printed nothing at all.

### What the errors actually cost, as recorded facts

The cost classes above are a model of what a wrong answer *reaches*. Beside them the report prints
two things the corpus simply **records**, with no model in between:

- **wrong on a TERMINAL failure — 18 of 26** for the historical incumbent, 9 of 26 at HEAD, 6 of 26
  at HEAD with the wide window. A terminal classification is the last word on that node; nothing
  downstream could correct it.
- **wrong on a node whose own artefact later scored a healthy metric — 10** for the historical
  incumbent, 0 at HEAD. These are the ten `rubertlite-dense-retrieval` nodes below.

Neither is weighted into anything. They are printed because "64.4% accurate" and "wrong on 18 of the
26 failures that ended a node" are the same measurement described at two very different altitudes.

### A cause the vocabulary cannot name

`Idea.params` is a proposal, not what ran. On **4 of the 122 rows** the stage's own argument parser
refused the parameters the engine substituted into its command (`train.py: error: unrecognized
arguments: --loss.temperature 0.05 --train.training.gradient_accumulation_steps 2 …`), so the
hyperparameters the node existed to test never reached a line of code. `crash` is still the honest
classification — the process exited non-zero at argv parsing and no member of `FAILURE_REASONS` says
anything else — so those rows carry `cause_notes.params_rejected_by_stage` **beside** the label and
never as one. A thirteenth reason would break the single property that lets this corpus claim the
classifier is wrong at all: its vocabulary is the classifier's own. The annotation is what an
agentic diagnostician can be asked to *name*, and what a substring rule cannot.

The silent twin is larger and this corpus is structurally blind to it. Measured across every run on
disk (2026-08-20): 457 comparisons of declared params against the node's own `config.yaml`, **41
diverged (9.0 %)**, 18 of them on nodes that *produced a metric* — the e5 champion at 0.793426 is
recorded as batch 8192 / accum 2 / 15 epochs and ran batch 512 / accum 32 / 3 epochs. A bench of
failures cannot see a defect whose whole shape is a node succeeding at the wrong experiment.

### Using it

```bash
python -m looplab.judgebench score-triage                        # the reason each run actually recorded
python -m looplab.judgebench score-triage --arm frozen          # the 2026-08-20 incumbent, durable stderr tail
python -m looplab.judgebench score-triage --arm frozen-widened  # ... with the triage agent's own log reads too
python -m looplab.judgebench score-triage --arm live            # _failure_reason at HEAD (deterministic half)
python -m looplab.judgebench score-triage --answers cand.jsonl  # a diagnostician's captured answers, offline
python -m looplab.judgebench extract-triage runs/* -o tests/data/judge_bench/failure_triage.v1.jsonl.gz
```

`--run <name>` scores one run's rows; `--high-confidence-only` drops the three medium-confidence
bases. `score-triage` makes **no network call**. A candidate is supplied as a JSONL of
`{"case_id", "reason"}`, which is what makes an agentic diagnostician scorable at all: it answers
once, offline, over the same recorded evidence.

### The dataset

`tests/data/judge_bench/failure_triage.v1.jsonl.gz` — schema `looplab.judgebench.failure_triage.v1`,
**122 rows, 93,202 bytes** gzipped. Committed, derived and never hand-edited, for the same three
reasons the monitor dataset is: `runs/` is not in the repository, a reviewer cannot diff a dataset
that does not exist, and `tests/test_triage_bench.py` re-derives every label from the row's own
stored facts. Every stored text goes through `core/redact.py::redact_output_tail` — the same screen
persisted output tails already pass.

Each row splits its evidence by **who could see it**: `evidence.at_classification` is what
`_failure_reason` itself had (the durable stderr tail, the failed stage's exit code, the attempt's
`stage_finished` rows), and `evidence.on_demand` is what a tool-using diagnostician could fetch and
a substring rule structurally could not (the `read_log` outputs the triage agent actually pulled,
and the failed stage's own log file). That split is not cosmetic — it is the only thing that lets
the bench say whether a candidate's win came from reading better or from looking further, and on
this corpus the answer was *looking further*.

The label vocabulary is `core/models.py::FAILURE_REASONS` plus `unknown`, imported rather than
copied: a bench whose vocabulary drifts from the classifier's cannot say the classifier is wrong,
only that it disagrees with a stale list. The two things it *does* copy — `TORCH_OOM_MARKERS` and
`NEVER_SALVAGED_REASONS` — are copied on purpose, because a bench that moves when the thing it
measures moves cannot detect that it moved; the test asserts each copy still agrees with its
original.
