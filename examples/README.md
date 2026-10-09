# Examples

Start with the offline demo — no model, no network, no Docker, a few seconds:

```bash
looplab run examples/demo.yaml          # then: looplab ui, and open the `demo` run
```

Every other file here is a task file. Run one with `looplab run examples/<file> --out runs/<name>`.
**Offline** means it also runs with `--backend toy` and no model: the synthetic kinds have a
model-free optimizer, and the others run their fixed baseline so you can check the pipeline (the
printed score is then marked as an offline baseline). **Model** means the interesting part — an
agent writing or editing code — needs a configured LLM (`looplab smoke` checks one).

| File | Kind | What it shows | Without a model |
|---|---|---|---|
| `demo.yaml` | `quadratic` | The whole loop on a closed-form objective; one file, no flags | Offline |
| `toy_task.json` | `quadratic` | The same objective as a bare task file | Offline (`--backend toy`) |
| `toy_task_noisy.json` | `quadratic` | The objective with evaluation noise | Offline (`--backend toy`) |
| `regression_task.json` | `regression` | Polynomial degree + ridge selection by 5-fold CV | Offline (`--backend toy`) |
| `classification_task.json` | `classification` | Feature-map degree + classifier tuning by CV | Offline (`--backend toy`) |
| `timeseries_task.json` | `timeseries` | A forecaster scored by a rolling-origin backtest | Offline (`--backend toy`) |
| `code_regression_task.json` | `code_regression` | The model writes the numpy fitting code | Offline baseline; model for code |
| `mlebench_task.json` | `mlebench` | Competition-shaped task with a private grader | Offline (`--backend toy`) |
| `mlebench_hostgraded_task.json` | `mlebench` | The same, graded on the host | Offline (`--backend toy`) |
| `dataset_task.json` | `dataset` | Point at `dataset_example/data.csv`; the model writes the solution | Offline baseline; model for code |
| `repo_task.json` | `repo` | Edit and tune the small repo in `repo_example/` | Offline baseline; model for edits |
| `repo_composable_task.json` | `repo` | The same through the composable `repo` + `cmd` schema | Offline baseline; model for edits |
| `repo_stages_task.json` | `repo` | A declared train → score pipeline with `%params%` | Offline baseline; model for edits |
| `repo_framework_task.json` | `repo` | Hyperparameters as CLI overrides, no code edits | Offline baseline; model to search |
| `repo_drift_task.json` | `repo` | A metric file cross-checked against stdout | Offline baseline; model for edits |
| `repo_onboard_task.json` | `repo` | Only a repo path: the agent proposes the evaluation | Model |
| `mlebench_real_spooky.json` | `mlebench_real` | A real Kaggle competition, official grader | Kaggle token + model |
| `mlebench_real_insults.json` | `mlebench_real` | A real Kaggle competition, official grader | Kaggle token + model |
| `mlebench_real_nomad.json` | `mlebench_real` | A real Kaggle competition, official grader | Kaggle token + model |
| `speculation_gate_seed_0.json` … `_2.json` | `quadratic` | Maintainer inputs for `looplab speculation-gate` | Offline |

Supporting folders: `dataset_example/` (the CSV the dataset task reads), `repo_example/` (the repo the
repo tasks edit), `knowledge/` and `skills/` (sample knowledge notes and a skill for
`knowledge_dir`). Every field of every kind is in the [task reference](../docs/guide/tasks.md).
