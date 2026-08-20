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
- **That last comparison is a confound, not an A/B.** The two slices are different runs with
  different failure mixes. Separating "tools helped" from "those runs were harder" needs the SAME
  rows replayed both ways — which is the live arm, below.
- **101 "missed stops" is not 101 mistakes.** Most are early looks at an attempt that failed later,
  and 38 of them watched a stage the engine failed *after* it exited, over artifacts the judged log
  never showed. Cut them with `--exclude-basis`, or read the per-attempt view, which asks the
  question an operator actually pays for: **7 of 27 wasted attempts caught, 3 of 49 productive
  attempts falsely stopped, ≈18.9 h of compute saveable.**

## Using it

```bash
python -m looplab.judgebench extract runs/*                 # rebuild the dataset from runs/
python -m looplab.judgebench score                          # the incumbent replaying itself
python -m looplab.judgebench score --answers candidate.jsonl # a candidate's captured answers, offline
python -m looplab.judgebench score --stage train --tools with
python -m looplab.judgebench score --exclude-basis stage_failed
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
never hand-edited — `tests/test_judge_bench.py::test_every_label_rederives` recomputes every label
from the row's own stored facts through the production rule, offline, so an edited label goes red on
a machine with no corpus; and `test_the_dataset_regenerates_from_the_runs_it_names` rebuilds it byte
for byte where the runs exist.

Every stored text goes through `core/redact.py::redact_output_tail` — the same screen persisted
output tails already pass, entropy pass included, not a second rule. Two honest limits:
`redact_env_values` screens the environment of the process running the *extractor*, which need not
be the environment the run had; and the split's byte-for-byte check is performed on the redacted
text.

## What this corpus cannot answer

- **Model choice.** One model produced all 450 verdicts.
- **`TrainingVerdict.fault`.** Not one recorded verdict carries it; the field postdates every
  preserved run.
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
| `--arm head` — `_failure_reason` at HEAD over the durable ~500-char stderr tail | **88/118 = 74.6 %** | 82.9 % |
| `--arm head-widened` — the same rule, plus the triage agent's own log reads | **104/118 = 88.1 %** | 69.2 % |

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
| `--arm head` | 38/42 = 90.5 % | **50/76 = 65.8 %** |
| `--arm head-widened` | 38/42 = 90.5 % | **66/76 = 86.8 %** |

Read the middle row before quoting the headline. HEAD's +10.2 points overall is almost entirely the
authenticated half — the `check_failed` branch existing, which is a real 2026-08 fix and is how the
ten `no_metric` terminals below got their right answer. **On the text-read half HEAD is 2 rows WORSE
than the classifier that actually ran**, and it only pulls ahead when it is handed evidence the
durable record does not contain. A single accuracy number hides that completely, which is the same
reason this bench refuses a weighted cost total.

### The sharpest finding: the marker rule's win is the window

`_is_torch_oom` (landed 2026-08-20) scores **0 of 23** OOMs replayed over the durable stderr tail,
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

At HEAD (`--arm head`) the shape moves rather than shrinking: `check_failed` 15/15, `crash` 50/51
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
python -m looplab.judgebench score-triage                      # the reason each run actually recorded
python -m looplab.judgebench score-triage --arm head           # _failure_reason replayed at HEAD, durable stderr tail
python -m looplab.judgebench score-triage --arm head-widened   # ... with the triage agent's own log reads too
python -m looplab.judgebench score-triage --answers cand.jsonl # a candidate's captured answers, offline
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
