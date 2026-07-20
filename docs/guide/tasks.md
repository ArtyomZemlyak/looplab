# Tasks

A **task** is a small JSON file telling LoopLab *what to optimize*. It's dispatched on a `kind`
field to a `TaskAdapter` (`looplab/adapters/tasks.py`). Pass it to `looplab run`:

```bash
looplab run path/to/task.json
```

## Common fields

Every task shares these:

| Field | Type | Description |
|---|---|---|
| `kind` | string | The adapter to use (table below). **Optional** — with the composable schema it's inferred from the fields; a `kind`-less task with no recognizable capability field is rejected (no silent quadratic default). The CLI's `--kind quadratic` offline default is separate. |
| `id` | string | A short identifier for the task (groups sibling runs) |
| `goal` | string | A natural-language objective; the agent reads this |
| `direction` | `min` \| `max` | Whether lower or higher metric is better |
| `seed` | int | Random seed for reproducible data generation |

## The composable schema (recommended)

You don't have to pick a `kind` — describe **what you have**, and the engine infers the task from
which capability fields are present (`looplab/adapters/tasks.py::normalize_task`):

| Field | Meaning |
|---|---|
| `repo` | Absolute path to an **editable codebase** — the agent may edit any file within it (`protect: [...]` for exceptions; default edit surface is everything). |
| `dataset` | Data / model weights that live outside the repo, as `{ "<mount>": "<abs path>" }` (a bare path mounts as `./dataset`). Read-only by default; a value may be an object with [per-source permissions](#per-source-data-permissions). They appear at `./<mount>` in the workdir. |
| `cmd` | **How to run + score** one experiment — either a bare argv `["python","test.py"]` or an object `{ "command"\|"stages", "metric": {"reader","key"}, "timeout" }`. This is the operator's **authoritative, non-rewritable** scorer. |
| `kaggle` | A Kaggle / MLE-bench competition slug (the official grader scores a submission — no `cmd` needed). |
| `benchmark` | A built-in synthetic task (`quadratic`, `regression`, …) for testing the loop. |

`metric.reader` is **how to read** the printed metric — `stdout_json` / `stdout_regex` /
`file_json` / `file_regex`, or `"auto"` to have the agent write the reader. The optimization
**direction** is the task's `direction`, never the reader.

```jsonc
{
  "goal": "maximize test recall@100 of a rubert-tiny-lite retriever",
  "direction": "max",
  "repo": "/home/me/dense-retrieval",
  "dataset": {
    "dataset_rubertlite": "/home/me/data/datasets/rubertlite",
    "embedder_rubertlite": "/home/me/data/embedder/rubert-tiny-lite"
  },
  "cmd": {
    "command": ["python", "test_looplab.py"],
    "metric": {"reader": "stdout_json", "key": "recall@100"},
    "timeout": 14400
  }
}
```

### `cmd` is a contract; edit-scope is separate

**`cmd` is the run+score CONTRACT** — the command that runs and the reader that reads its metric. It is
the *scoring* step, not the trainer: **the Developer declares training (and any prep) as separate stages
in a dedicated STAGES phase** — the first of its three phases (**stages → plan → implement**), run
before it writes any code and skipped only when the operator pre-empts it (a declared `cmd.stages`
pipeline, or a protected `looplab_stages.json`); the engine runs those stages BEFORE `cmd`. The
cmd-context rule: if `cmd` is
present it is **immutable**, so the Developer declares only the **preceding** stages (data-prep, train)
and the operator's `cmd` is appended as the protected final `score` stage; if `cmd` is absent the STAGES
phase declares the **full** pipeline including a final scoring stage; if `cmd` itself declares `stages`,
those are canonical. Separate **prep / train / test** stages are recommended (a fresh model every node).
Put `%params%` in any command to inject the node's hyperparameters as `--key value`. Stage lists are
validated by ONE shared rule set (`runtime/command_eval.py::validate_stages`) at authoring (the STAGES
phase's `declare_stages` emit), submit (`cmd.stages`) and consume time (the engine re-validates even a
hand-written `looplab_stages.json`; `score` is reserved in a Developer manifest, and an invalid manifest
falls back to the single command instead of half-running).

**What the agent may EDIT is a separate, independent decision** — `edit_surface` (globs the agent may
edit; default = the whole repo) minus `protect` (exceptions). The engine does **not** auto-protect the
file `cmd` runs, so: if `cmd` points at an operator-owned scorer the agent must not change (e.g. the
framework's `test.py`), add that file to `protect`; if the scorer must be *built*, leave it editable (a
protected file can't be created).

### Per-source data permissions

Each `dataset` (or legacy `data`) value may be a bare path (all defaults) or an object with five
independent flags. **Default: everything allowed EXCEPT editing the original.**

```jsonc
"dataset": {
  "raw":  { "path": "/data/train",
            "mount": true,        // (1) read-only symlink at ./raw (default) | false = copy INTO the workdir
            "edit": false,        // (2) modify the data in place? default false. edit:true implies
                                  //     mount:false — a mount is read-only to the agent, so
                                  //     mount:true + edit:true is COERCED to a writable copy
            "copy_modify": true,  // (3) copy it and modify the copy
            "preprocess": true,   // (4) preprocess / augment / feature-engineer into a training set
            "extend": true },     // (5) extend / expand the data
  "test": "/data/test"            // a bare path = all defaults
}
```

**What is mechanically enforced.** `mount` and `edit` have enforced semantics; flags (3)–(5) are
**advisory** — they shape the allow-list in the agent's brief but no gate checks them.

- A **mounted** source is a read-only symlink at `./name`: it is protected against writes (`name` +
  `name/**`) in the Developer's write gate and the external agent's diff gate, and — under the
  `untrusted`/`hostile` tiers — is bind-mounted into the eval container **read-only**, so even code the
  eval *runs* (a declared `train` stage, a subprocess) physically cannot mutate the original. The
  agent's own build-time writes under `./name` would escape the workdir and be dropped anyway, so the
  gate refuses them **visibly** instead of letting the edit silently no-op. Because of that,
  `mount:true` + `edit:true` is **coerced to `mount:false`** (a writable per-node copy — what `edit:true`
  actually wants) rather than rejected, so a mounted original can't be silently edited AND pre-existing
  runs whose snapshot carried the combo still load. The `trusted_local` tier runs on the host, where only
  the write/diff gates apply — treat a read-only mount there as a guard against the *agent's edits*, not a
  hard sandbox.
- A **`mount:false`** source is a physical per-node **copy** inside the workdir: writes cannot reach the
  original, so the copy is writable (the brief calls it "a writable copy") — this is how you give the
  agent data it may preprocess/modify. On a CoW filesystem (btrfs/XFS) the per-node copy is a reflink
  clone (~free); on ext4 it is a full byte copy per node — budget disk accordingly for a large dataset.
- Declaring the same mount name in **both** `data` and `dataset` is rejected at submit time (one path
  would silently shadow the other). A `kaggle` slug **overwrites** a legacy `competition` value riding
  along in the same dict.
- For a **dataset**-kind task (no repo), permission objects are flattened to their `path` — the
  mount/edit machinery is repo-task infrastructure; the dataset kind reads data by absolute path.

Every legacy spelling still works — `{"kind":"repo","editable_path":...,"eval":{...,"metric":{"kind":...}},"onboard":...}`
parses unchanged, so old task files and snapshots keep running (`examples/repo_task.json` is the
legacy form; `examples/repo_composable_task.json` the composable form; and
`examples/repo_stages_task.json` shows a declared `cmd.stages` train→score pipeline with `%params%`
tuning and a per-source `dataset` permission object).

## The nine kinds (internal / legacy view)

The composable fields above desugar to these adapter kinds; you can still set `kind` explicitly.
`kind` is the **legacy** spelling (the `examples/*.json` catalogue files keep it, and it parses
unchanged); the composable form is `benchmark` for the built-in synthetics and capability fields
(`repo` / `dataset` / `cmd` / `kaggle`/`competition`) for everything else — the inline examples
below use it.

| `kind` | The agent's job | Metric source | Example |
|---|---|---|---|
| [`quadratic`](#quadratic) | Pick numeric params | Closed-form objective | `examples/toy_task.json` |
| [`regression`](#regression) | Select model complexity | K-fold CV (built-in) | `examples/regression_task.json` |
| [`classification`](#classification) | Tune a classifier | K-fold CV (built-in) | `examples/classification_task.json` |
| [`timeseries`](#timeseries) | Tune a forecaster | Backtest (built-in) | `examples/timeseries_task.json` |
| [`code_regression`](#code_regression) | **Write the code** | CV printed by the solution | `examples/code_regression_task.json` |
| [`mlebench`](#mlebench) | Beat a private grader | Held-out grader | `examples/mlebench_task.json` |
| [`mlebench_real`](#mlebench_real) | **Real Kaggle competition** | Official grader | `examples/mlebench_real_spooky.json` |
| [`repo`](#repo) | Edit an existing repo | The repo's **own** eval | `examples/repo_task.json` |
| [`dataset`](#dataset) | **Write the whole solution** on your data | Self-reported (agent-chosen) metric | `examples/dataset_task.json` |

---

## `quadratic`

A toy numeric objective — the offline default. The Researcher proposes points; there's no code
generation. Good for learning the loop and testing crash-resume.

```jsonc
{
  "id": "toy_quadratic",
  "goal": "minimize (x-3)^2 + (y+1)^2 ; optimum at x=3, y=-1, loss=0",
  "direction": "min",
  "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]},
  "seed": 7,
  "step": 1.5
}
```

| Field | Description |
|---|---|
| `bounds` | Map of `name → [low, high]` numeric search bounds |
| `step` | Hill-climb step size for the toy proposer |

## `regression`

Polynomial-degree + ridge-λ model selection, scored by K-fold cross-validation built into the
adapter. The loop converges on a sensible model complexity.

```jsonc
{
  "benchmark": "regression", "id": "poly_regression",
  "goal": "select polynomial degree + ridge lambda minimizing 5-fold CV MSE (true degree 2)",
  "direction": "min",
  "n": 40, "true_degree": 2, "noise": 1.0, "seed": 1, "max_degree": 6, "cv_k": 5
}
```

| Field | Description |
|---|---|
| `n` | Number of generated samples |
| `true_degree` | The data-generating polynomial degree |
| `noise` | Gaussian noise level |
| `max_degree` | Largest degree the search may try |
| `cv_k` | Cross-validation folds |

## `classification`

Tune a classifier (e.g. logistic regression) for K-fold CV accuracy on generated blob data.

```jsonc
{
  "benchmark": "classification", "id": "blob_classification",
  "goal": "tune a logistic-regression learner to maximize K-fold CV accuracy",
  "direction": "max",
  "n": 80, "sep": 1.5, "seed": 0, "cv_k": 5
}
```

| Field | Description |
|---|---|
| `n` | Number of samples |
| `sep` | Class separation (lower = harder) |
| `cv_k` | Cross-validation folds |

## `timeseries`

Choose a forecaster's smoothing weight + seasonal period to minimize backtest error (MASE).

```jsonc
{
  "benchmark": "timeseries", "id": "seasonal_forecast",
  "goal": "choose a forecaster's smoothing weight + seasonal period to minimize backtest MASE",
  "direction": "min",
  "n": 120, "period": 7, "trend": 0.05, "noise": 0.5, "seed": 0,
  "max_period": 12, "backtest_h": 20
}
```

| Field | Description |
|---|---|
| `n` | Series length |
| `period` | True seasonal period |
| `trend` | Trend slope |
| `max_period` | Largest period the search may try |
| `backtest_h` | Backtest horizon |

## `code_regression`

Same problem as `regression`, but the **LLM writes the code**: a complete numpy script that reads
the dataset from a materialized `data.json` asset, fits the model, runs CV, and prints the metric.
Requires `--backend llm`. When a generated script crashes, the self-repair operator fixes it.

```jsonc
{
  "benchmark": "code_regression", "id": "code_poly_regression",
  "goal": "write code (numpy) that fits a polynomial+ridge model to data.json minimizing 5-fold CV MSE; true degree 2",
  "direction": "min",
  "n": 40, "true_degree": 2, "noise": 1.0, "seed": 1, "max_degree": 6, "cv_k": 5
}
```

Same data fields as `regression`.

## `mlebench`

A competition-shaped task with **leaderboard grading**: the solution gets `train.json` (X + labels)
and `test.json` (X only — labels withheld) and must call a private `grader.score(preds)`, so the
loop optimizes the *true held-out* metric, not a self-reported one. The grader is asset-name
protected so the agent can't overwrite it.

```jsonc
{
  "benchmark": "mlebench", "id": "mlebench_blobs",
  "goal": "train a classifier on train.json and maximize held-out accuracy on test.json (private grader)",
  "direction": "max",
  "seed": 0, "n_train": 80, "n_test": 40, "n_features": 4, "sep": 2.0, "noise": 1.0, "max_k": 15
}
```

## `mlebench_real`

Run an **actual Kaggle competition** from OpenAI's [MLE-bench](https://github.com/openai/mle-bench):
the engine provides the official `public/` split, the solution writes `submission.csv`, and the
**host** scores it with MLE-bench's real grader against held-out answers — producing the genuine
MLE-bench metric plus the official medal / above-median report.

```jsonc
{ "competition": "spooky-author-identification" }
```

| Field | Description |
|---|---|
| `competition` | The MLE-bench competition slug |

This needs the competition data prepared first. See the full **[MLE-bench runbook](../MLEBENCH.md)**
(Kaggle token, per-competition rule acceptance, the untrusted tier).

```bash
python -m looplab.adapters.mlebench_prep --selected            # download + prepare CPU-lite comps
looplab run examples/mlebench_real_spooky.json --out runs/spooky --backend llm
```

## `repo`

Point the R&D agent at an **existing repository**. It edits code within an allow-listed surface, and
success is the **repo's own eval command + metric** — never a metric the agent authored.

```jsonc
{
  "id": "repo_example",
  "goal": "tune config.json to maximize the eval metric (max at x=3)",
  "direction": "max",
  "repo": "examples/repo_example",               // the repo the agent edits (worktree copy)
  "edit_surface": ["*.json"],                    // … only files matching these globs
  "protect": ["ttrain.py"],                      // … never the eval entrypoint
  "cmd": {
    "command": ["python", "ttrain.py"],
    "metric": {"reader": "stdout_json", "key": "metric"},
    "timeout": 60
  }
}
```

| Field | Description |
|---|---|
| `editable_path` | Path to the repo; mounted into each eval workdir (a worktree copy). `~`/`$VARS` expand |
| `edit_surface` | Globs the agent may edit **or create** (reject-not-strip) |
| `protect` | Files the agent may **never** touch (e.g. the eval entrypoint) |
| `eval.command` | The command run to evaluate a candidate (**argv list, no shell** — no `&&`) |
| `eval.setup` | Optional command run **before** each eval to install **dependencies** (e.g. `pip install -r requirements.txt`). **Not for training** — training is a stage the agent declares (see below). |
| `eval.metric.reader` | How to read the metric: `stdout_json` / `stdout_regex` / `file_json` / `file_regex` / `auto`. (Legacy `eval.metric.kind` still works.) |
| `eval.metric.key` | The JSON key / regex / file path to read |
| `eval.metric.resource_key` | Optional JSON key for an explicit training resource (for example `step`). ASHA live kill compares only observations carrying the same declared resource value; without it endpoint ranking is advisory only. |
| `eval.timeout` | Per-eval timeout (seconds) — set it generously for training (often 7200–14400) |
| `data` / `dataset` | `name → path` map, **read-only symlink-mounted** at `./name` by default; a value may be a [per-source permission object](#per-source-data-permissions). `~`/`$VARS` expand |
| `references` | Read-only inputs: `[{name, path, mount}]` — `mount: true` copies to `./name`, `false` is context-only |
| `editables` | Multi-repo workspace: extra editable repos, each mounted at its own `name/` subdir |

The metric-source file and the files you list in `protect` cannot be overwritten by the agent
(enforced by the write/diff gate); the scorer entrypoint is protected only if you `protect` it. Offline or
on agent failure, a no-op developer leaves the repo unmodified.

> **Have a test/eval but no training script?** Set `cmd` to the scorer (`["python","test.py"]`) and
> `protect` it — the Developer declares a `train` **stage** in its dedicated STAGES phase (the first of
> its three phases: stages → plan → implement; skipped only if you declare `cmd.stages` yourself or
> protect `looplab_stages.json`) that runs before the scorer, then the engine
> trains and your protected `cmd` scores the freshly-trained model. Do **not** run training via
> `eval.setup` (that's for dependency installs and reruns every eval). See
> **[Generating train & test code](generating-code.md)** for this and every other "let the agent write
> the code" case — and the **Genesis** flow that authors the whole spec from a plain-text goal.

### Framework mode (tune with no code edits)

Set `params_style: "cli_overrides"` and declare a hyperparameter space — the Researcher's proposals
become `key=value` CLI overrides on the eval command (Hydra-style). Add **eval profiles** for a
cheap `smoke` during search and a `full` run on confirmation:

```jsonc
{
  "repo": "examples/repo_example", "direction": "max",
  "protect": ["ttrain_cli.py"], "params": {"x": [-5.0, 5.0]},
  "cmd": {
    "command": ["python", "ttrain_cli.py"],
    "params_style": "cli_overrides",
    "metric": {"reader": "stdout_json", "key": "metric"},
    "profiles": {
      "smoke": {"overrides": ["steps=10"],  "timeout": 60},
      "full":  {"overrides": ["steps=200"], "timeout": 120}
    }
  }
}
```

### Onboarding (let the agent figure out the eval)

Set `"onboard": true` and give the framework's command — the agent **writes a metric adapter** for
whatever tracker the repo uses (TensorBoard / MLflow / ClearML / a metrics file / stdout), proposes
the eval, a human **ratifies it once** with `looplab approve`, and then it's frozen + protected. The
trust policy is `eval_trust_mode` (`ratify_freeze` default / `autonomous` / `ratify_freeze_drift`).

```bash
looplab run examples/repo_onboard_task.json --backend llm \
    --developer-backend opencode --model qwen3:8b
# run pauses with a proposed eval+adapter; review it, then:
looplab approve runs/run_local
looplab resume  runs/run_local --task-file examples/repo_onboard_task.json
```

---

## `dataset`

The fully-generative *"here is my data — write the whole solution and get the best metric you see
fit"* task. You bring only a **data path** and a goal; the LLM Developer writes a **complete solution
from scratch** each iteration (read the data → build a model → evaluate → print the metric), and the
self-repair operator fixes crashes. Requires `--backend llm`. Offline it falls back to a deterministic
baseline that just reports the dataset row count, so the engine still runs without a model.

```jsonc
{
  "id": "dataset_example",
  "goal": "predict `target` from the features; pick the metric you judge most appropriate",
  "direction": "max",
  "data_path": "examples/dataset_example/data.csv",   // your data (file or dir); ~/$VARS expand → absolute
  "seed": 0
}
```

| Field | Description |
|---|---|
| `data_path` | Path to your data (file or directory). Resolved to an absolute path the solution reads directly |
| `data` | Optional extra named paths (`name → path`) for multi-file datasets |
| `metric` | Optional metric **name** to optimize; leave empty to let the agent **choose** one (and report its `metric_name`) |
| `direction` | `max` (default) / `min`. The agent reports the metric with that orientation (higher- or lower-is-better) |
| `cv_k` | Cross-validation folds the brief suggests for honest evaluation |

**Self-chosen metric.** With no `metric` set, the agent picks the most appropriate one (accuracy / F1 /
AUC / R² / …) and prints both `metric` and `metric_name`. With `direction: "max"` it reports a
higher-is-better value (an error metric is negated), so selection stays consistent.

**Trust caveat.** Like `code_regression`, the solution **self-reports** its own metric — there is no
private grader, so this trades the anti-cheat guarantee for zero-setup convenience (the reward-hack /
code-leakage monitors still audit it). For the hard *"the agent never authors its own metric"*
guarantee, use [`repo`](#repo) (your own eval command) or [`mlebench_real`](#mlebench_real) (held-out
grader). **Data access** is by absolute path, which works under the default `trusted_local` tier; for
the `untrusted`/`hostile` docker tiers mount the data via a `repo` task instead (an absolute host path
isn't visible inside the container).

```bash
looplab run examples/dataset_task.json --backend llm --max-nodes 8
```

---

## Writing your own task

Any object exposing `id`, `goal`, `direction`, and `build_roles()` is a valid `TaskAdapter`
(optionally `columns()` to enable the grounding/profiling pre-phase). For built-in kinds you only
write JSON; for a new kind, add an adapter to `looplab/adapters/tasks.py`'s `_KINDS` registry. See
[Concepts](concepts.md) for how a task plugs into the loop.
