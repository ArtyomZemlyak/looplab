# 47 — What the early-stop machinery misses, and which of it is fixable (2026-08-20)

Re-derived from `tests/data/judge_bench/train_monitor.v1.jsonl.gz` and from the preserved `runs/` on
this box. Every number here is reproducible from those two sources; nothing is quoted from an
earlier note.

The corpus is 450 recorded decisions under one judge model. Its header's `sources` list names SIX
runs (`e5small-dr-unified-v2` 168, `-v3` 2, `rubertlite-dr-unified-v6` 43, `-v7` 38, `-v8` 135,
`-v9` 64) while its own `CORPUS_LIMITS` paragraph — the caveat that travels with every printed
report — says seven. Six is what the file contains; the discrepancy is noted rather than corrected,
because `CORPUS_LIMITS` is pinned by `tests/test_judge_bench.py` against the committed header and
changing it is a dataset edit, not a doc edit.

## 1. The measurement

`python -m looplab.judgebench score`, grouped by `label_basis` and then — inside `stage_failed` — by
the exit status of the stage row each decision was actually about:

| what happened | decisions | judge said `broken` |
|---|---|---|
| trained fine, metric ~0 (`node_metric_degenerate`) | 53 | **48 — 91 %** |
| stage exited 0, engine failed it on a CHECK | 23 | 1 — 4 % |
| stage exited 0, engine failed it on ARTIFACTS (`expect_failed`) | 15 | 1 — 7 % |
| stage KILLED by signal -9 | 39 | 2 — 5 % |
| stage KILLED by signal -15 | 2 | 0 |
| stage CRASHED with an exception (exit 1) | 22 | 1 — 5 % |

The judge is right when the failure is in the CURVE and appears blind everywhere else.

**The headroom.** An oracle that says `broken` on the first decision of every wasted attempt the
incumbent MISSED saves 20.10 h across 20 attempts. Decomposed:

| class | attempts | oracle hours | share |
|---|---|---|---|
| exit 0, `check_failed` | 4 | 9.20 | 45.8 % |
| exit 0, `expect_failed` | 3 | 4.25 | 21.1 % |
| exit 1, crash | 11 | 4.96 | 24.7 % |
| signal -9 | 1 | 0.88 | 4.4 % |
| signal -15 | 1 | 0.81 | 4.0 % |

## 2. Class B — killed by a signal (41 decisions). REFUSED: there is no miss in it.

Every one of those 41 decisions belongs to four attempts, and each attempt was ended by the engine's
own deterministic machinery:

| attempt | stage seconds | ended by | decisions |
|---|---|---|---|
| `rubertlite-dr-unified-v7` n0 train | 29,389.5 | the STALL watchdog (`node_repaired reason=stalled`) | 19 |
| `rubertlite-dr-unified-v7` n1 train | 29,184.2 | the STALL watchdog | 17 |
| `e5small-dr-unified-v2` n5 train | 3,780.5 | the DIVERGENCE watchdog (`node_repaired reason=diverged`) | 3 |
| `rubertlite-dr-unified-v7` n0 train | 3,499.9 | a run-level cancellation (SIGTERM) | 2 |

The two `-15` rows landed at the SAME instant (2026-08-14 15:46:02) for two different nodes, 43
minutes before the run finalized: that is the engine tearing down both live evals, not an experiment
failing. So the "5 %" on this class is not blindness — 39 of 39 signal kills were the system working
and the remaining 2 were the operator stopping the run. A new "the log went silent" signal would
duplicate `_tee_drain`'s stall watchdog and could only add false stops.

## 3. Class C — crashed with an exception (22 decisions). The stated premise is FALSE.

The claim to check was "the traceback is in STDERR, while the judge reads the TRAINING LOG". It is
not: `runtime/sandbox.py::_tee_drain` starts one `_pump` per stream and BOTH write to the same
`logf`, so `<stage>.log` is a combined stdout+stderr tee. Verified on six real crash logs — v9 n0,
v9 n6, v2 n2, v2 n7, v8 n3 `mine.log`, v8 n10 — all six contain `Traceback (most recent call last)`,
with `ModuleNotFoundError`, `AssertionError`, `AttributeError` and `NameError` among them, in the
file the judge reads and that `tools/log_tools.py::read_log`'s search mode already sweeps to the end
of. Nothing here needs a new stream.

What is actually true about this class is different and cheaper to say: a crash is
**self-terminating**. The process is already dead when the traceback exists, the engine acts on it
at exit through `_triage_crash`, and the only way an earlier stop could realise the 4.96 h is if the
crash were PREDICTABLE from earlier output. That is a real question and it is left open
(the open marker in `looplab/engine/train_monitor.py`, slug
`crash-predictability-unmeasured`), because it needs a
measurement nobody has taken: where in each attempt's byte range the first traceback lands.

## 4. Class A — the stage exited 0 (38 decisions, 13.45 h, 67 % of the headroom)

This is where the money is, and it splits.

### 4a. `check_failed` (9.20 h) — FIXED, as evidence

All four missed attempts are `declared_condition_violated`. Three of them declared an epoch count
the trainer's own configuration could never reach, and each ECHOED that configuration in the first
30 KB of a multi-hour log:

| attempt | declared | trainer configured | stage seconds | byte offset of the echo |
|---|---|---|---|---|
| v8 n8 train | 15 | `"n_epochs": 8` | 14,105.1 | 29,182 of 5,875,029 |
| v8 n9 train | 10 | `"n_epochs": 6` | 8,354.8 | 3,735,890 (start of that attempt) |
| v9 n1 train | 50 | `"n_epochs": 1` | 3,933.3 | 6,952 |
| v9 n1 train | 50 | `"n_epochs": 1` | 4,001.1 | 2,043,225 (start of that attempt) |

The engine already held the promise — `stage.expect.assert`, the same sentence
`command_eval.py::epoch_floor_acquits` reads at the END — and never showed it to the live judge.
`train_monitor.stage_contract_context` now does, with a deterministic reading of the schedule the
trainer configured (`epoch × total / done` from the trainer's own progress records).

### 4b. `expect_failed` (4.25 h) — REFUSED: not decidable at monitor time

Three attempts, and the workdirs say why none of them was knowable early:

* v9 n3 train (8,364.5 s) declared `"full 15-epoch schedule"`, echoed `"n_epochs": 15`, ran all
  5,295 steps and printed `{'train_runtime': 8216.3}` — a complete training that then did not write
  `final/model.safetensors`. Everything up to the final `save` was correct.
* v6 n3 train (5,118.5 s) and v8 n0 `mine` (3,662.9 s and 3,613.0 s) are the same shape.

An artifact's absence is only evidence after the writer has had its chance. The one variant that
looked decidable — comparing the declared path against the directory the stage is filling — is a
prediction about a path the candidate has not written yet, and v8 n0's own manifest shows why it
would have been wrong in both directions (the declared `experiments/nllcos_hn/negatives.parquet`
differs from the written `experiments/nllcos_hn_rubert-tiny-lite/negatives.parquet` only because
`vectorsearch/config.py::run_name` appends the backbone, which the engine cannot know).

## 5. The operator's second question: "do we always stop a run whose gradient broke or whose loss exploded?"

**No, and on this corpus that is the right default.** Four findings, each driven.

**(a) Why the four `rubertlite-dense-retrieval` divergences never fired the watchdog: the watchdog
did not exist.** `_StageHealthMonitor` landed in `2d8c6736` on 2026-07-18; that run's last event is
2026-07-18 03:18:52. Not the threshold, not the clearing rule, not the parser, not the arming
condition. Replaying the SHIPPED `_StageHealthPair` over the preserved logs settles the parser
question directly — it handles the tqdm `loss=inf` shape and fires on node 15 inside the first
64 KiB chunk, against a stage that ran 4,335 s.

**(b) The parser is fine; the arming is the one real gap.** `_run_single` passes no `health_check`,
so a single-command RepoTask eval — which IS the training, as that branch's own comment says — has
no deterministic divergence kill at all. The open marker for it lives on that branch
in `looplab/runtime/command_eval.py`, slug `single-command-eval-has-no-divergence-watchdog`.

**(c) Two of the four "divergences" were not divergences, and this is what refuses the obvious fix.**

| node | first loss | last loss | recorded | metric |
|---|---|---|---|---|
| n15 | `inf` (step 1) | `inf` | `no_metric` | — |
| n60 | finite → `nan` at epoch 55 | `nan` | `no_metric` | — |
| n68 | -1.5e+10 | -2e+10 (flat, `rdrop_loss` → 0.000) | `no_metric` | — |
| n74 | **-2.44e+06** | **-2.32e+08** | `no_metric` | — |
| **n48** | **-2.44e+06** | **-2.32e+08** | evaluated | **0.8835 — the run's champion** |

n74 and n48 agree to three significant figures at every sampled fraction of their logs
(-2.72e+07/-2.73e+07 at 2 %, -1.81e+08/-1.80e+08 at 25 %, -2.32e+08 at the end). n39 opens at a
friendly `10.2`, ends at `-9.74e+08` and scored 0.8654. Measured over all 249 stage logs on this
box, **28 reach |loss| ≥ 1e8 and 26 of them produced a metric**, sixteen above 0.87. What condemned
n74 was an end-of-stage LLM checker reading a big negative number.

**(d) `_anomaly_of`'s explosion rung is structurally blind to a negative explosion — and correcting
it is measurably worse than leaving it.** It tests `abs(window.maximum)`, the SIGNED max, which for
an all-negative loss is the value NEAREST zero. Driven on the real logs, node 74 measures
`direction=descending, anomaly=''`, so `trajectory_vetoes_kill` returns **True**: a run the
end-of-stage checker called diverged is read by the engine as a healthy descending curve and granted
immunity from every `broken` verdict for the rest of the node.

The correction is refused permanently, because every variant fails on a different node of the same
four. One 20-tick windowing of each whole log, `_anomaly_of`'s own arithmetic:

| node | outcome | opening median | shipped ratio | magnitude-symmetric ratio | peak \|loss\| |
|---|---|---|---|---|---|
| n74 | condemned "diverged" | -4.04e+07 | 5.64× | 6.29× | 2.54e+08 |
| **n48** | **0.8835, the champion** | -4.00e+07 | **5.65×** | **6.33×** | **2.53e+08** |
| n39 | 0.8654 | 7.71 | 35,538,262× | 127,626,459× | 9.84e+08 |
| n68 | `no_metric` | -2.00e+10 | 1.00× | 1.00× | 2.00e+10 |

A magnitude bar cannot separate n74 from n48 at any value. Neither can the ratio — and the
champion's ratio is the HIGHER of the two. The ratio ALREADY fires on n39, which runs 7.71 →
-9.84e+08 and scored 0.8654, so the shipped 100× boundary is a false positive waiting on that node's
shape. And nothing ratio-based can ever see n68, the one node plausibly broken on its own terms
(`rdrop_loss` collapses to 0.000), because it opened diverged and never moved. An `anomaly` can only
make things END, so a bar that catches the condemned node kills the champion. The ratios are
windowing-dependent and are quoted with their derivation; the ORDERING is what refuses the rung.
The permanent decline is recorded beside
`_anomaly_of` in `looplab/engine/train_monitor.py`, slug
`explosion-rung-cannot-be-magnitude-symmetric`.

## 6. What was built, and its effect on the corpus

`Settings.train_monitor_contract` (ON) →
`train_monitor.stage_contract_context`: the watched stage's own `expect.assert` and `expect.files`,
plus a deterministic shortfall reading, spliced into the user message of a tick the monitor already
pays for. Zero extra provider calls. It widens EVIDENCE only — `should_monitor_kill`,
`should_monitor_repair`, the kill-eligible roles and the trajectory veto are untouched (docs/36).

The deterministic half is scorable offline. Scored over all 450 rows through the engine's own
confidence gate, with the declared epoch target joined from each node's `looplab_stages.json` (the
committed corpus does not carry it; that join is the only thing in this table not reproducible from
the dataset file alone):

Both gate arms, because they differ and the difference is itself a finding:

| gate | candidate | accuracy | false stop | missed stop | true stop | true continue | wasted attempts caught | productive attempts stopped | approx saveable |
|---|---|---|---|---|---|---|---|---|---|
| confidence + trajectory veto (shipped `Gate()`) | incumbent | 0.703 | **0** | 105 | 49 | 200 | 6 / 27 | 0 / 49 | 18.10 h |
| " | schedule rung alone | 0.573 | **0** | 151 | 3 | 200 | 3 / 27 | 0 / 49 | 3.15 h |
| " | incumbent OR rung | 0.709 | **0** | 103 | 51 | 200 | 8 / 27 | 0 / 49 | 20.80 h |
| confidence only | incumbent | 0.703 | **0** | 105 | 49 | 200 | 6 / 27 | 0 / 49 | 18.10 h |
| " | schedule rung alone | 0.599 | **0** | 142 | 12 | 200 | 4 / 27 | 0 / 49 | 6.47 h |
| " | incumbent OR rung | **0.734** | **0** | 94 | 60 | 200 | **9 / 27** | **0 / 49** | **24.12 h** |

Confusion for the combined arm: `productive/broken 5`, `productive/healthy 164`,
`productive/watch 31`, `wasted/broken 64`, `wasted/healthy 52`, `wasted/watch 38`. The five
`productive/broken` rows are the incumbent's and all five sit below the 0.8 confidence bar, which is
why the gated false-stop column is 0 in every arm.

Two things the table has to say about itself. The rung fires on **12 of 450 decisions, 12 wasted and
0 productive** (the schedule is readable on 298 of the 450 digests; where it is not, the rung says
nothing and the block carries only the declaration). And the trajectory veto is what separates the
two gate arms: it suppresses the rung on v8 n8 (7 decisions, 11,940 s) because that node's loss
really was descending — correct for the veto's own question and irrelevant to this one, since a
descending loss does not make an 8-epoch schedule reach 15. That is a note about a conjunct this
rung is deliberately NOT wired into. **It is also the argument against ever wiring it in**: the
moment a schedule reading became a kill conjunct it would inherit a veto that answers a different
question, and the fix for that would be a second veto policy. As evidence it needs neither.

**Negative controls that decided the shape**, both driven in `tests/test_train_monitor_contract.py`:

* the step counter and the epoch must come from ONE record. 109 of the 109 corpus stage logs above
  200 KB carry more than one progress-bar lane, and the loose pairing reads v8 node 13 — a completed
  10-epoch training that scored 0.716575 — as a 4.02-epoch schedule, by pairing a finished `313/313`
  dataloader bar with a training log dict. One false stop, taken to zero.
* the tolerance is `command_eval.DECLARED_EPOCH_TOLERANCE`, shared rather than copied, so the live
  reading can never call a stage short that `epoch_floor_acquits` would acquit. Over v9 node 0's 11
  recorded decisions (14.87 of a declared 15) it reads 14.87-14.92 and fires on none.

## 7. The guard that was written to prevent this exact failure, and did not

`tests/test_train_monitor_trajectory.py::test_the_loop_and_the_verdict_helper_agree_on_the_positional_contract`
exists because adding a parameter to `_training_verdict` twice before left
`test_train_monitor.py`'s stubs one short: a stub the loop's call does not fit raises `TypeError`
INSIDE the monitor's own per-tick `except Exception: continue`, so the monitor spins and the test
HANGS instead of failing. Its docstring says so, names both previous times, and ends "Keep it in
step with the signature."

It counted POSITIONAL arguments. Splicing `contract_text=contract_text` as a KEYWORD left that count
at 5, every assertion in it stayed green, and `test_monitor_cancellation_joins_the_paid_verdict_worker`
became a 15-minute hang for the THIRD time — first mistaken for a loaded box, then bisected to 8
tests in, then to the stub. **The guard named the mechanism its first two breakages happened to use,
not the property.** It now takes the call's positional count AND its keyword names out of the AST and
`inspect.Signature.bind`s them against the real method and against every stub the suite substitutes;
three mutations (either stub losing the parameter, and the real method losing it while the loop still
passes it) each produce a red test in under a second instead of a hang.

Two things worth keeping from that: a harness whose failure mode is a HANG hides the diagnosis behind
whatever else is slow that hour — the load average on this box was 8 while it was being investigated,
and "the box is loaded" is a fully sufficient wrong explanation. And a guard-test docstring naming
its own past failures is not the same as a guard-test that would catch the next one.
