# The MLE-bench Lite campaign — the protocol, the columns, and the number this box has not produced

*2026-09-06. Doc 52 row 23, marker `no-external-benchmark-number-exists`. This page holds the
PROTOCOL and the reviewer's columns; the campaign needs GPUs, a model endpoint and the prepared
Kaggle data, none of which the authoring session had. The results section is filled in by the box
run, one dated line per competition, and doc 52's marker is deleted in the same change.*

## 0. Why the number did not exist

`adapters/mlebench_real.py` and `docs/MLEBENCH.md` shipped the real host-graded path, and no
completed run was recorded anywhere in the tree. Four things blocked an honest number, and each is
now in the tree: the search optimised the private grade until doc 52 row 3 carved an agent-invisible
split (`holdout_fraction`, one private grade at finish); no seed protocol existed until row 23
(`docs/MLEBENCH.md`, ≥3 seeds mean ± SEM); the grader recorded no percentile rank until row 23
(`mlebench_grade.py::percentile_rank`); and neither official detector had a counterpart until row 22
(`looplab mlebench-extras`). What remains is the wall-clock.

## 1. The protocol

1. Prepare each competition once (`docs/MLEBENCH.md`, "Prepare the data"); accept its rules on
   kaggle.com; download its public kernels into `kernels/<competition>/` for the plagiarism pass
   and set `kernels_dir` on the task.
2. For each competition, **≥3 seeds** with the product `Settings`, the same model and endpoint, the
   same `--max-nodes` and time budget — the `for seed in 1 2 3` loop in `docs/MLEBENCH.md`.
3. `looplab mlebench-extras runs/<c>-s<seed>` for every run (the rule-violation judge + Dolos).
4. `python -m looplab.adapters.mlebench_campaign runs/<c>-s*` per competition — the table below —
   and `looplab export-bundle runs/<c>-s<seed>` for every run, so the reviewer bundle exists for
   each number reported.
5. Report the raw mean ± SEM AND the Mislead-adjusted mean ± SEM beside it, the private-grade mean,
   the mean percentile, medal / above-median rates, and the rule-violation count — never a single
   run's best, and never the adjusted number alone.

## 2. The reviewer's columns (the survey's Table 10)

| column | where it comes from |
|---|---|
| code + prompts | the bundle's `champion/`, `config.snapshot.json` (every prompt-changing flag is a Settings field), the PromptStore overrides if any |
| seeds / traces | `summary.json::seeds`, `events.jsonl`, `spans.jsonl` in the bundle |
| result-selection policy | `holdout_select` / `trust_gate` / `metric_salvage` in the snapshot; `best_metric_caveats` and `mislead_gap` on the summary row |
| novelty method | `novelty_gate` / `graded_novelty` in the snapshot; the `novelty_*` rows in the log |
| HITL entry points | `require_approval` and the control events (`approval_granted`, `node_abort`, …) in the log |
| harness + cost | `looplab tokens` / `looplab timings` over the run; the `llm_usage` ledger in the log |
| hack-adjusted number | `mislead_gap` per run; the campaign table's adjusted column; `mlebench_extras.json` per run |

## 3. What a number from this campaign is evidence for

One model, one box, the Lite subset, three seeds: a claim about THIS deployment on THESE
competitions, read beside AIRA₂ / OpenAI on the percentile scale only. The adjusted column is a
LOWER bound on protocol validity (the filter sees hard trust signals and salvage, not rule-compliant
shortcuts — those are `docs/audit/developer-hack-rate.md`'s question).

## 4. Results

Not produced. One dated line per competition, from the aggregator's table
(`RESULT <date> <competition>: runs=… seeds=… raw=… ± … adjusted=… ± … private=… percentile=…
medal_rate=… violations=…`), with the bundle directories named beside it.
