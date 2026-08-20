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

`judge_corpus.py` builds the training-log monitor's dataset and `score.py` replays a candidate over
it; `triage_corpus.py` + `triage_score.py` are the same two verbs for the FAILURE CLASSIFIER
(`engine/triage.py::_failure_reason` and whatever replaces it), which is outcome-labelled for a
different reason — the run says what the failure really was in the repair that followed, the reset
that reused the condemned stage output, and the allocator's own words in the log. `python -m
looplab.judgebench` is the entry point for both (`score` / `extract` and `score-triage` /
`extract-triage`).

The failure bench also carries the one thing an accuracy number cannot: **the COST of each error**.
Its answer selects a repair directive, gates the dependency install and meets the salvage refusal,
so `crash`-for-`oom` (a wasted round) and `oom`-for-`diverged` (rounds spent moving the wrong dial)
are not the same mistake. `triage_score.ERROR_COSTS` names them and there is deliberately no
weighted total.

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
    Gate, ScoreReport, attempt_totals, per_attempt_report, score_dataset)
from looplab.judgebench.triage_corpus import (
    LABEL_BASES, LABEL_UNKNOWN as TRIAGE_LABEL_UNKNOWN, LiveRunRefused,
    build_dataset as build_triage_dataset, derive_label as derive_triage_label,
    extract_run as extract_triage_run, read_dataset as read_triage_dataset,
    rederive_label as rederive_triage_label, write_dataset as write_triage_dataset)
from looplab.judgebench.triage_score import (
    ERROR_COSTS, cost_of, head_replay_candidate, score_dataset as score_triage_dataset)

__all__ = [
    "ERROR_COSTS", "LABEL_BASES", "LiveRunRefused", "TRIAGE_LABEL_UNKNOWN",
    "build_triage_dataset", "cost_of", "derive_triage_label", "extract_triage_run",
    "head_replay_candidate", "read_triage_dataset", "rederive_triage_label",
    "score_triage_dataset", "write_triage_dataset",
    "CORPUS_LIMITS", "DATASET_SCHEMA", "Gate", "LABELS", "LABEL_BUDGET_EXHAUSTED",
    "LABEL_PRODUCTIVE",
    "LABEL_UNKNOWN", "LABEL_WASTED", "ScoreReport", "build_dataset", "extract_run", "messages_of",
    "per_attempt_report", "attempt_totals", "read_dataset", "score_dataset",
    "write_dataset",
]
