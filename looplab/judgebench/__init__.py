"""Judge benches — measure a prompt or model change against RECORDED decisions instead of on faith.

Every LLM judge in this engine (`engine/train_monitor.py`, `engine/triage.py`,
`engine/repair_verify.py`, `search/novelty.py`) is a prompt that was edited unmeasured: the only
evidence a change helped has been that the next run felt better. The runs already hold what a bench
needs — `spans.jsonl` records both the `input` and the `output` of every `generation` span — so the
missing piece was never the data, it was the LABEL.

**The label is the whole design problem, and it does not exist for every judge.** Two disjoint
classes, and this package refuses to blur them:

* **Outcome-labelled.** The run itself later says whether the judgement was right, in a fact the
  judge did not author: the training-log monitor called a node `broken` and the node then scored
  0.0; the crash triage chose repair and the repair either landed or hit the same wall. Here an
  agreement number is EVIDENCE ABOUT CORRECTNESS.
* **Unlabelled.** The novelty gate REJECTS an idea, so the idea is never run and nothing on disk
  says whether it would have worked. There is no outcome, and there never will be. A candidate can
  only be scored for CONSISTENCY with the old verdict there — and "agrees with the old model" is a
  score that is maximised by reproducing the old model's mistakes. **Never present a consistency
  number as an accuracy number.** `score.py` keeps the two in separate fields for that reason and
  has no code path that averages them.

`judge_corpus.py` builds the dataset (currently the training-log monitor only); `score.py` replays a
candidate over it. `python -m looplab.judgebench` is the entry point.

**Deliberately not a `looplab` CLI subcommand**, for the same reason `looplab/sweep.py` is not one:
this is a developer tool over the operator's local `runs/` directory, and nothing on a run's own
execution path should be able to reach it.

**`judgebench`, not `bench`**: `looplab/bench.py` is the D2 CAPABILITY self-benchmark (does the
engine still solve these tasks end to end) and owns `looplab bench`. This measures a JUDGE against
recorded decisions. A package named `bench` shadowed that module and broke its import — the two
answer different questions and the names have to say so.

## What a number from this corpus is evidence FOR

Seven runs, ONE task family (ESCI dense retrieval, two backbones), ONE operator, ONE judge model
(`deepseek-v4-flash` in 450 of 450 recorded monitor decisions). A bench optimised against this
corpus will overfit to it. A number measured here is evidence about THIS deployment on THIS task —
it is not a general claim about the judge, the prompt, or the model, and it must not be quoted as
one. `CORPUS_LIMITS` states this in the dataset header itself, so the caveat travels with the file.
"""
from __future__ import annotations

from looplab.judgebench.judge_corpus import (
    CORPUS_LIMITS, DATASET_SCHEMA, LABELS, LABEL_BUDGET_EXHAUSTED, LABEL_PRODUCTIVE,
    LABEL_UNKNOWN, LABEL_WASTED, build_dataset, extract_run, messages_of, read_dataset,
    write_dataset)
from looplab.judgebench.score import (
    ScoreReport, attempt_totals, per_attempt_report, score_dataset)

__all__ = [
    "CORPUS_LIMITS", "DATASET_SCHEMA", "LABELS", "LABEL_BUDGET_EXHAUSTED", "LABEL_PRODUCTIVE",
    "LABEL_UNKNOWN", "LABEL_WASTED", "ScoreReport", "build_dataset", "extract_run", "messages_of",
    "per_attempt_report", "attempt_totals", "read_dataset", "score_dataset",
    "write_dataset",
]
