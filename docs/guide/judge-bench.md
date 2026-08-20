# Judge bench — measuring a prompt change instead of believing it

Every LLM judge in this engine is a prompt someone edited on faith. The training-log monitor's kill
authority was changed twice in one week and the only evidence either change helped was that the next
run felt better. This page is about the thing that ends that: a **frozen corpus of recorded
decisions with outcome labels**, and a scorer that replays a candidate over it.

!!! warning "What a number from this corpus is evidence for"

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
- **failure triage** (`engine/triage.py`) — it named a cause and chose repair-or-abandon, and what
  followed says whether the repair landed. Not built, and **smaller than it looks**: 1,634 recorded
  `triage` generation spans are only **104 decisions**, because triage is agentic and one decision
  is a ~16-turn tool loop. Count decisions, never spans — the same collapse takes the monitor's
  3,950 spans down to 450.
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
