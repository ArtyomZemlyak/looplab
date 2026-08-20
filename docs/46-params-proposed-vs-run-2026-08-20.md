# The parameters in the record are not the parameters that ran

*2026-08-20. The root cause of the recurring parameter failures, measured end to end on live data.
Doc 45 established the SHAPE — a claim recorded in one place whose truth lives in another with
nothing connecting them. This is that shape's most expensive instance, and the one the operator has
been burning on.*

## The measurement

`Idea.params` compared to the node's own `vectorsearch/configs/config.yaml`, matched on the full
dotted path, or on a dotted SUFFIX when that suffix resolves to exactly one path. A leaf-only match
would be worthless here — bare `batch_size` resolves to three different paths
(`test.retriever.model_settings`, `train.training`, `adapter.training`) and would compare a proposal
against a set it does not belong to. A bare name is a word, not a path; ambiguity is skipped, never
guessed.

* **457 comparisons, 41 DIVERGED — 9.0%.** Five skipped as ambiguous.
* **18 of the divergences sit on nodes that produced a metric** — the population a champion is
  picked from.
* The YAML carrier covers **484 of 512 declared proposals (94.5%)**: 283 at the exact declared path,
  201 under a longer path (`loss.temperature` declared, `train.loss.temperature` in the file), and
  only 28 with no YAML leaf at all. So reading this one carrier answers the question for ~95% of
  everything the Researcher declares.

The scored ones, where a champion can be picked:

| run | node | metric | proposed | RAN |
|---|---|---|---|---|
| `e5small-dr-unified-v2` | **1 (champion)** | 0.793426 | batch 8192 · accum 2 · 15 epochs | **batch 512 · accum 32 · 3 epochs** |
| `e5small-dr-unified-v2` | 6 | 0.774207 | same | same |
| `e5small-dr-unified-v2` | 9 | 0.792082 | same | same |
| `rubertlite-dr-unified-v8` | 12 | 0.761400 | batch 8192 · lr 0.001 · 12.5 epochs · temp 0.05 | **batch 2048 · lr 0.0005 · 1 epoch · temp 0.005** |
| `rubertlite-dr-unified-v8` | 9, 13, 14 | 0.761773 / 0.716575 / 0.752719 | 10 epochs | **15 epochs** |

Note which direction v8's 9/13/14 go: they trained LONGER than the record says. This is not a
one-way "the Developer shrinks things to fit" story, which is why a rule of thumb cannot substitute
for reading the file.

Caveat, stated because it bounds the number: the config on disk is a node's **final** state. For a
node that was repaired after scoring, the comparison is against bytes the metric did not see. Read
41 as "declared values whose final state disagrees with the record", not as "metrics computed with
the wrong parameters".

## This is not the Developer being wrong

The v2 champion's own committed YAML carries its reasoning inline:

```yaml
n_epochs: 3      # cut from 15: 3x703 steps x ~10.8s/it ~ 6.3h fits the 10h budget with
                 # margin; e5-small v2 benchmark nears ceiling at ~5 epochs
batch_size: 512  # per-device batch (halved again to fit H200 under R-Drop's 8 concurrent forwards;
                 # 32x accumulation -> eff 16384)
gradient_accumulation_steps: 32   # effective batch 16384 (512 x 32)
```

That is exactly the behaviour we want: the thing that runs on the machine discovered the machine's
limit and adjusted, preserving the effective batch and noting where the benchmark plateaus. The
defect is not the deviation. **The defect is that the record keeps the proposal.**

`params_style: "none"` is the reason the deviation is legitimate: the engine applies nothing, and the
Developer realises the proposal by editing the repo. But `Idea.params` is then read as fact by the
Strategist, by the next run's Researcher through the cross-run capsules, by the champion's own
recipe, and by the operator.

## Why the guard that exists produced a FALSE CLEAN

`engine/repair_verify.py::declared_param_overrides` exists for precisely this question and drives a
champion caveat at `engine/champion_caveats.py:223`. Line 905:

```python
if not isinstance(text, str) or not str(path).endswith(".py"):
    continue
```

Run directly against the v2 champion's committed bytes, it returns **empty**. The five files it was
handed include `vectorsearch/configs/config.yaml` (17,706 bytes) — the very file holding `batch_size:
512` and `n_epochs: 3`. It skipped it on the extension.

And the `.py` it *does* read makes the answer worse than a miss. `vectorsearch/config.py:417` is
`temperature: LossTemperature = 0.05` — the pydantic **schema default**, equal to the declared
proposal, while the YAML that overrides it says `0.01`. So the detector finds agreement and reports
**no override**: a confident clean about a run using a different number.

The champion caveat is recomputed from the fold rather than stored, so no event search can prove it
never fired — it has to be re-run, and it was.

## The compounding path, in order

1. The Strategist proposes batch 8192.
2. The engine applies nothing (`params_style: "none"`); the Developer realises it and deviates to 512
   × 32 to fit memory, documenting why.
3. `declared_param_overrides` reads only `.py`, matches the schema default, reports clean.
4. `node_created` records 8192 / 2 / 15 and the metric 0.793426 is attributed to it.
5. The operator reads the record and writes 8192 into the v3 goal. **Three nodes die of OOM.**
6. v4's agent reads the same record, proposes 8192 / 2 / 15 to "reproduce the champion recipe". Its
   Developer again deviates on batch (4096 × 4) but takes the **15 epochs** from the record — five
   times the champion's actual three.

No participant in that chain is stupid. There is a field everyone reads as fact, and a guard looking
at the wrong carrier.

## A second carrier problem, same shape

`vectorsearch/config.py::settings_customise_sources` returns `(init, YAML, env, dotenv, CLI)`, and
pydantic-settings takes the FIRST source as highest priority. So environment variables and
command-line overrides are **silently ignored** for any key the YAML defines. An agent that sets a
parameter on the command line sees the command succeed and gets the old value.

## What the fix has to be

Read the carrier the task actually uses, and bind the **applied** configuration at the metric read,
beside the proposed one — then surface the divergence rather than refusing it. A node that adjusted
for a real constraint must still be allowed to run and to win; what must not survive is a record that
attributes its number to parameters it never used.

The machinery exists: `runtime/metric_inputs.py` binds real inputs at the metric read through
`metric_subject.bind_one`, and the eval already writes its resolved config to
`…/tests/final/config.yaml`. The deciding site is on disk and readable.

`OPEN[declared-params-guard-reads-only-py]` — the guard for declared-vs-applied parameters skips every
carrier that is not Python, and matches a pydantic schema default while the YAML overrides it.
proof:present:endswith(".py")@looplab/engine/repair_verify.py
