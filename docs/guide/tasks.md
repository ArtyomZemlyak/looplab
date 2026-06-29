# Tasks

A **task** is a small JSON file telling LoopLab *what to optimize*. It's dispatched on a `kind`
field to a `TaskAdapter` (`looplab/tasks.py`). Pass it to `looplab run`:

```bash
looplab run path/to/task.json
```

## Common fields

Every task shares these:

| Field | Type | Description |
|---|---|---|
| `kind` | string | The adapter to use (table below). Defaults to `quadratic` if omitted |
| `id` | string | A short identifier for the task (groups sibling runs) |
| `goal` | string | A natural-language objective; the agent reads this |
| `direction` | `min` \| `max` | Whether lower or higher metric is better |
| `seed` | int | Random seed for reproducible data generation |

## The eight kinds

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
  "kind": "regression", "id": "poly_regression",
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
  "kind": "classification", "id": "blob_classification",
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
  "kind": "timeseries", "id": "seasonal_forecast",
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
  "kind": "code_regression", "id": "code_poly_regression",
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
  "kind": "mlebench", "id": "mlebench_blobs",
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
{ "kind": "mlebench_real", "competition": "spooky-author-identification" }
```

| Field | Description |
|---|---|
| `competition` | The MLE-bench competition slug |

This needs the competition data prepared first. See the full **[MLE-bench runbook](../MLEBENCH.md)**
(Kaggle token, per-competition rule acceptance, the untrusted tier).

```bash
python -m looplab.mlebench_prep --selected            # download + prepare CPU-lite comps
looplab run examples/mlebench_real_spooky.json --out runs/spooky --backend llm
```

## `repo`

Point the R&D agent at an **existing repository**. It edits code within an allow-listed surface, and
success is the **repo's own eval command + metric** — never a metric the agent authored.

```jsonc
{
  "kind": "repo", "id": "repo_example",
  "goal": "tune config.json to maximize the eval metric (max at x=3)",
  "direction": "max",
  "editable_path": "examples/repo_example",     // the repo the agent edits (worktree copy)
  "edit_surface": ["*.json"],                    // … only files matching these globs
  "protect": ["ttrain.py"],                      // … never the eval entrypoint
  "eval": {
    "command": ["python", "ttrain.py"],
    "metric": {"kind": "stdout_json", "key": "metric"},
    "timeout": 60
  }
}
```

| Field | Description |
|---|---|
| `editable_path` | Path to the repo; mounted into each eval workdir (a worktree copy) |
| `edit_surface` | Globs the agent may edit (reject-not-strip) |
| `protect` | Files the agent may **never** touch (e.g. the eval entrypoint) |
| `eval.command` | The command run to evaluate a candidate |
| `eval.metric.kind` | How to read the metric: `stdout_json` / `stdout_regex` / `file_json` / `file_regex` |
| `eval.metric.key` | The JSON key / regex / file path to read |
| `eval.timeout` | Per-eval timeout (seconds) |

Eval and protected files cannot be overwritten by the agent (enforced by construction). Offline or
on agent failure, a no-op developer leaves the repo unmodified.

### Framework mode (tune with no code edits)

Set `params_style: "cli_overrides"` and declare a hyperparameter space — the Researcher's proposals
become `key=value` CLI overrides on the eval command (Hydra-style). Add **eval profiles** for a
cheap `smoke` during search and a `full` run on confirmation:

```jsonc
{
  "kind": "repo", "direction": "max", "editable_path": "examples/repo_example",
  "protect": ["ttrain_cli.py"], "params": {"x": [-5.0, 5.0]},
  "eval": {
    "command": ["python", "ttrain_cli.py"],
    "params_style": "cli_overrides",
    "metric": {"kind": "stdout_json", "key": "metric"},
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

## Writing your own task

Any object exposing `id`, `goal`, `direction`, and `build_roles()` is a valid `TaskAdapter`
(optionally `columns()` to enable the grounding/profiling pre-phase). For built-in kinds you only
write JSON; for a new kind, add an adapter to `looplab/tasks.py`'s `_KINDS` registry. See
[Concepts](concepts.md) for how a task plugs into the loop.
