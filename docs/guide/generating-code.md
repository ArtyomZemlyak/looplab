# Generating the train & test code

LoopLab can write the experiment code for you — either **fully from scratch** (you bring only a goal,
data, and a metric) or by **editing/extending your own repository** (the agent writes the missing
pieces inside an allow-listed surface, your own command scores it). This page covers **every case**
and the JSON behind each.

The fastest way to set any of this up is **[Genesis](#preferred-let-genesis-author-it)** — the
"New run" chat in the Web UI. It authors the whole spec and, for a repo, has read-only scout tools
plus instructions to inspect your code; tool use is model-directed, so verify the card. Every case
below shows both the Genesis path and the equivalent task file.

> **New composable schema.** A task no longer needs a `kind` — describe what you HAVE (`repo`,
> `dataset`, `cmd`, `kaggle`, `benchmark`) and the engine infers the rest (see [Tasks → the composable schema](tasks.md#the-composable-schema-recommended)). The legacy `kind`/`editable_path`/`eval`/`onboard`/`metric.kind`
> spellings below all still work (they're accepted as aliases), but the composable names are preferred.
> Three things worth knowing for a **training** repo:
> - **`cmd` is the SCORING step, not the trainer.** The Developer declares training (and any prep) as
>   separate stages in a **dedicated STAGES phase** it runs *first* (before plan + implement; skipped
>   only when you pre-empt it — declare `cmd.stages` yourself, or protect `looplab_stages.json`);
>   the engine runs those stages, then appends your `cmd` as the final protected `score` stage over the
>   freshly-trained model. Do **not** put training in `cmd.setup` (that's for dependency installs). If
>   your repo has an existing scorer (`test.py`), pass it as `cmd` and `protect` it; if the scorer must be
>   built, point `cmd` at a file the agent will create and don't protect it.
> - **Give a training `cmd.timeout` generously** (often `7200`–`14400`s) — the 600s default SIGKILLs a
>   long train into an undertrained checkpoint.
> - **`%params%`** in a command expands to the node's tuned hyperparameters (`--key value`); **per-source
>   data permissions** — a `dataset` value may be `{path, mount, edit, copy_modify, preprocess, extend}`
>   (default: read-only original, but the agent may copy/preprocess/extend it into a training set).

> **On the command line**, Genesis runs headless too: `looplab run --goal "<what you want>"` lets the
> LLM author the task from your words — it picks the kind *and* reads where your data lives (one path
> or several, a file or a folder), so you needn't pass `--data`. Pin the kind with `--kind` and Genesis
> fills the rest within it; add `--no-genesis` to build the task from flags alone. It's the same idea as
> the UI planner, minus the editable card.

---

## Two ways the agent writes code

| | **From scratch** | **Edit your repo** (`kind: repo`) |
|---|---|---|
| You bring | a goal + data + a metric | an existing repo + a way to run/score it |
| Who writes the train/test code | the **Developer writes the whole script** every iteration | the agent **edits/creates files** inside `edit_surface`; everything else is yours |
| Metric source | a held-out grader the engine owns (`mlebench`, `mlebench_real`) — **but not every from-scratch kind: see the trust note below** | **your own `eval.command`** (a trust boundary — the agent never authors it) |
| Kinds | `code_regression`, `mlebench`, `mlebench_real` | `repo` (+ its onboarding / framework variants) |
| Use when | there is no code yet — a Kaggle-style "data in, predictions out" problem | you already have a project and want it improved/completed in place |

> `classification`, `regression`, and `timeseries` also run with an LLM, but they **tune knobs in a
> fixed template** rather than writing free-form code. The "writes the whole script" kinds are
> `code_regression`, `mlebench`, `mlebench_real` and `dataset`.

!!! warning "Which kinds actually have a held-out grader"

    Only **`mlebench`** (private grader holding the answer key) and **`mlebench_real`** (the official
    competition grader) give the *"the agent never authors its own metric"* guarantee. **`code_regression`
    does not**, despite being a from-scratch kind: it is a **demo** task in which the solution computes
    **and self-reports** its own K-fold CV MSE, with no private grader — so a reward-hacking model could
    print a fake number (`adapters/regression.py:195-199`). `dataset` self-reports too
    ([tasks.md](tasks.md#dataset)). If you are choosing a kind **for the anti-cheat guarantee**, use
    `mlebench` / `mlebench_real`, or `repo` (your own protected `eval.command` + protected metric
    reader). The code-leakage / reward-hack monitors still *audit* the self-reporting kinds, but auditing
    is not a grader.

---

## Preferred: let Genesis author it

The main-menu **"New run"** chat in the [Web UI](ui.md) is the owner Assistant's planning surface.
You describe the goal in plain text; its `propose_run` tool returns an **editable run card** — a name,
the task spec, the key settings (model,
node budget, seeds, policy), and, for a repo, an **adaptation checklist** of what to do to make the
project LoopLab-ready. Nothing launches until you confirm; refinement may still consume Assistant
model calls even though it has not started a scientific run.

The CLI keeps the historical **Genesis** task author, and the TUI calls the server's `/api/genesis`
planner. These three surfaces share task-adapter validation and backend-default authority, but they
do not share one planner/schema. Web additionally submits a reviewed `/api/start/preflight` token;
the TUI asks `/api/validate` (the same funnel, answered as a verdict) on every draft and binds its
`/api/start` to the token it returns; CLI validates directly. Refine a proposal in the surface that
created it.

For a repo it is a real **agent** with read-only scout tools (`list_dir` / `read_file` /
`find_files`) and is instructed to inspect your README, entry/eval script, requirements and result
files before authoring. Tool use is model-directed rather than a hard gate, so verify the editable
card's command, metric key and edit surface before launch.

### What Genesis writes vs what the run writes

Genesis writes the **plan, not the experiment code**. Keep these two roles separate:

- **Genesis plays the *operator*** (you, automated for the planning step): it authors the run **spec**
  — the `eval.command`, `edit_surface`, the metric reader, settings, and the adaptation checklist. So
  yes, *it writes the commands itself*, the way you would. It is instructed not to invent filesystem
  paths; commands remain proposals, so verify them. For an empty repo it may propose a convention or
  ask a clarifying question.
- **The run's agent writes the *code***. The actual `train.py` / `run.py` / `config.yaml` are written
  by the Developer **during the run**, inside `edit_surface`.

**No scripts yet?** That's fine. Genesis sets `eval.command` to e.g. `["python","run.py"]` and puts
`run.py` in `edit_surface` (so it's allowed to be *created*). On the first (draft) node the agent
**creates** the missing script(s); the eval runs them from then on. Genesis wrote the command — the
agent writes the file behind it.

**Truly no repo — just data + a goal?** Genesis can instead pick a **from-scratch generative kind**
(`code_regression` / `mlebench_real`), where the Developer writes the *entire* solution each iteration
and you bring only the data + metric — cleaner than a `repo` task with an empty surface. See
[I have only a goal + data](#i-have-only-a-goal-data-generate-everything).

The one thing to pin down either way is the **metric**: if you can't yet say how it's scored, Genesis
sets `onboard: true` (the agent proposes a metric adapter, you ratify it) or bakes the convention
(*"print `{"metric": <score>}`"*) into the checklist so the agent's generated script emits it.

**How to use it**

1. Open the UI (`looplab ui`), choose **New run**.
2. Describe the goal and, for your own project, **give the repo path** and say **how it's run and how
   it's scored** — e.g. *"Optimize the model in `~/proj/ranker`; run it with `python train.py`, it
   prints a final line `{"metric": <ndcg>}`, maximize that."*
3. Review/edit the surfaced card fields, choose **Validate**, inspect the effective preview and
   warnings, then choose **Start** when the validated card is enabled.
4. Edit surfaced fields directly on the card. For a change the card does not surface, ask the
   Assistant for a revised proposal; it appears as a new card rather than mutating the old one.

**Tell it the specifics it can't guess** — copy paths/commands/metric keys verbatim in your message;
the Assistant is instructed to preserve them rather than invent filesystem paths. State these
explicitly, then verify the proposal:

- **Where your data lives** — say *"the data is at `~/proj/data`, mount it as `dataset`"* and Genesis
  authors the `data` mount; omit it and there's nothing to mount. See
  [Pointing at your data](#pointing-at-your-data).
- **A non-stdout metric** — if the metric isn't printed as JSON on stdout, say where it ends up (a
  file, TensorBoard/MLflow); Genesis switches to a `file_json`/`file_regex` reader or to
  [onboarding](#i-can-run-it-but-dont-know-how-its-scored-onboarding).
- **No scripts / args-or-config-driven / hyperparameter-only** — just describe it; Genesis now knows
  these patterns (it'll have the agent create a missing script, drive it via a config the agent edits,
  or set up `cli_overrides` for a pure tuning run).

---

## Case by case

| Your situation | Mode | Section |
|---|---|---|
| Only a goal + data, no code at all | from-scratch generative kind | [↓](#i-have-only-a-goal-data-generate-everything) |
| A repo with working code **and** a way to score it | `repo` edit mode | [↓](#i-have-a-repo-with-code-and-an-eval-edit-mode) |
| A **test/eval but no training script** | `repo` edit mode (agent writes `train.py`) | [↓](#i-have-a-test-but-no-train-the-agent-writes-it) |
| Can run it, but the score isn't exposed | `repo` **onboarding** | [↓](#i-can-run-it-but-dont-know-how-its-scored-onboarding) |
| Only want to tune hyperparameters, no code edits | `repo` **framework** mode | [↓](#i-only-want-to-tune-knobs-no-code-edits-framework-mode) |

### I have only a goal + data → generate everything

The Developer writes the **entire** solution (train + inference) each iteration and the self-repair
operator fixes crashes. You provide a goal, the data, and a metric — no code.

- **Just point at your data, let the agent pick the metric** → [`dataset`](tasks.md#dataset). The
  fully-generative *"here is my data, get the best metric you see fit"* kind: you give a `data_path`
  and a goal, the agent writes the whole solution and **chooses the metric itself** if you don't name
  one. Zero scaffolding.
  ```jsonc
  { "goal": "predict `target`; pick the metric you judge best",
    "direction": "max", "data_path": "~/proj/data.csv" }
  ```
  Trade-off: the metric is **self-reported** (no private grader). For the anti-cheat guarantee use a
  `repo` task or a held-out grader instead.
- **Real Kaggle problem** → [`mlebench_real`](tasks.md#mlebench_real): the official split + grader.
  Just `{"competition":"<slug>"}` (or `kaggle`); see the
  [MLE-bench runbook](../MLEBENCH.md) for data prep.
- **A held-out metric the agent cannot author** → [`mlebench`](tasks.md#mlebench) (train/test split +
  a private grader the agent can't overwrite).
- **A self-contained coding demo, self-reported metric** → [`code_regression`](tasks.md#code_regression)
  (LLM writes a numpy script that reads a materialized data asset, fits, cross-validates, prints the
  metric). It has **no private grader** — see the trust warning above before picking it for rigor.

*Genesis:* describe the goal and where the data is — for *"here's my data, find the best metric"* it
authors a `dataset` task; for a Kaggle competition it sets the `mlebench_real` slug.

### I have a repo with code and an eval → edit mode

The canonical [`repo`](tasks.md#repo) task: the agent edits files matching `edit_surface`, and
success is **your own** `cmd` (the scorer) + metric. The file the metric is **read from** (a
`file_json`/`file_regex` path, or an onboarding adapter) is auto-protected so the agent can't fake a
score. **The scorer entrypoint is auto-protected too, when your `cmd` names a file the repo already
ships** (`python -m pkg.mod` / `python eval.py`) — `cmd.protect_entrypoint: false` hands it back to
the agent, and a `cmd` naming no in-repo file (a wrapper script, a console script) warns at submit
because nothing there *can* be frozen. A scorer your repo does **not** ship is still the agent's to
author, which is the whole point of the "I have a test but no train" case below.
Listing it in `protect` as well is harmless and explicit. Composable form:

```jsonc
{
  "goal": "improve validation AUC", "direction": "max",
  "repo": "~/proj/model",                    // your repo (a worktree copy is mounted per eval)
  "edit_surface": ["src/**/*.py"],           // only these files may change
  "protect": ["eval.py"],                    // the agent must never touch the scorer
  "cmd": {
    "command": ["python", "eval.py"],
    "metric": {"reader": "stdout_json", "key": "metric"},  // eval prints {"metric": <score>}
    "setup": ["pip", "install", "-r", "requirements.txt"], // dependency install only
    "timeout": 1800
  }
}
```

*Genesis:* point it at the repo and state the run/score command. It has scout tools and instructions
to ground the proposal in your README/entry script; verify the editable card before validation.

### I have a test but no train → the agent writes it

Common case: you have a `test.py`/`eval.py` that scores a model, but **no training script**. `cmd` is
the SCORING step, not the trainer — **the Developer declares training as a stage in its dedicated
STAGES phase** (the first of its three phases: **stages → plan → implement**; skipped only when you
declare `cmd.stages` yourself or protect `looplab_stages.json`), which the
engine runs BEFORE your `cmd`. You do **not** put training in `cmd.setup` (that's for dependency
installs). Just set `cmd` to your scorer and `protect` it:

```jsonc
{
  "goal": "train a model that maximizes the test metric", "direction": "max",
  "repo": "~/proj",
  "edit_surface": ["**/*.py"],               // the agent CREATES train.py + edits code here…
  "protect": ["test.py"],                     // …and may never edit the scorer (contract)
  "cmd": {
    "command": ["python", "test.py"],         // your trusted scorer reads the trained checkpoint
    "metric":  {"reader": "stdout_json", "key": "metric"},
    "timeout": 14400                           // GENEROUS — it covers the train stage the agent adds
  }
}
```

Each iteration the Developer's STAGES phase declares a `train` stage (baking this node's
hyperparameters into it), then its implement phase writes the `train.py` that stage runs; the engine
runs `train` → appends your protected `cmd` as the final `score` stage → reads the metric from its
stdout. A crashed node is repaired IN PLACE (inline repair) and re-evaluated: when the fix touched
**only** the failed stage's code (e.g. a broken `score`/eval script that didn't change `train.py` or
anything it imports), the completed `train` checkpoint is **reused** and only `score` re-runs — no
re-train. If the repair changed the training code (`train.py`/`loss.py`/`model.py`/…), the node
re-trains from scratch (a stale checkpoint must never be scored — each node scores the model it
actually trained this run). The reuse check is strictly fail-closed: it must *prove* the earlier
stages' inputs are unchanged, so a repair that **deletes** any file, changes any **non-`.py`** file
(a config/params/data file — invisible to import reachability), or runs under a non-default
`cmd.cwd` also forces a full re-run, as does an opaque stage — a shell wrapper, a bare binary, or a
`python -m <module>` naming code the engine cannot find inside the workdir. A `python -m pkg.mod`
whose module DOES resolve to a workdir file is bounded like any other script (its entry file, its
package `__main__.py`, and everything they import), so it no longer blocks reuse on its own.
A change to the **stage manifest** (`looplab_stages.json`) forces a full re-run when it touches the
pipeline at or before the reuse point — the entries that already ran, their order, their names, or
their declared `expect`/`needs`/`env`/`timeout`. Since 2026-08-18 it does *not* when the edit is
confined **strictly after** it: retuning the failed `train` stage's argv cannot alter what the
completed `mine` stage produced, and the two manifests the engine itself committed prove that entry
byte-identical. The compare is over the stage ENTRY, not the file: it subsumes reorders and renames,
and inserting or removing a stage in front of the failed one still forfeits (a removed stage leaves
its outputs on disk, which is precisely the stale input this check exists to refuse).
`inline_repair_retrain_cap` bounds how many full re-trains a repair loop
may burn before abandoning the node. (It used to abandon *to* the inter-node debug operator; that
operator was removed on 2026-08-13 — a failure is repaired inside the node that failed, for as long
as the repair judgment allows.)

**A chain whose FIRST stage keeps failing discards nothing, so it spends none of that count** — and
since 2026-08-15 it spends the same cap in **seconds** instead. Declare a `timeout` on every stage:
that is the number the engine multiplies by `cap + 1` to decide how much evaluation wall-clock one
node's repair chain may redo. Without a declared timeout there is no licensed number and no bound —
the engine will not invent a pipeline cost in order to abandon your node.

**When the stage that broke is not the stage that is wrong**, the repair can say so. An earlier stage
that exited 0 was counted successful, but exit 0 only means it did not crash — a data/mining stage can
"succeed" having produced a fraction of what it should have, and the failure then surfaces one stage
later. A repair session's `done` emit carries `rollback_stage`: name that earlier stage and the engine
re-runs the pipeline **from** it, discarding its output and everything built on it. Three things bound
it, so a wrong guess cannot become an expensive loop: the repair must also have **changed** something
that stage runs or imports (naming a stage you did not fix is refused — re-running it unchanged would
produce the same bytes); each suspect stage may be rolled back to **at most once per node** (read back
off the event log, so a resume does not refund it); and every accepted rollback is charged to
`inline_repair_retrain_cap`, the same budget a forced full re-train spends. If the suspect stage's
script is one you `protect`, the refusal says so and names your file — the agent cannot fix what the
write gate refuses it, and that is a message for you, not a task for it.

Notes:

- Give `cmd.timeout` room for the whole schedule (often `7200`–`14400`s) — the 600s default SIGKILLs a
  long train into an undertrained checkpoint.
- Keep `test.py` in `protect`: `cmd` says *how it's scored*; `protect` says *what may not be edited* —
  two separate decisions.
- If `test.py` doesn't print the metric, read it from a file (`"metric": {"reader": "file_json",
  "path": "result.json", "key": "metric"}`) or use [onboarding](#i-can-run-it-but-dont-know-how-its-scored-onboarding).

*Genesis:* tell it *"there's a `test.py` that scores but no trainer"*; it sets `cmd` to the scorer,
protects it, and states in the goal that the agent must add a training stage.

### I can run it, but don't know how it's scored → onboarding

When you know the command that runs the project but the metric is buried in some tracker
(TensorBoard / MLflow / ClearML / a metrics file / stdout), set **`"onboard": true`** and give the
command. The agent **writes a metric adapter** (`read_metric(workdir) -> float`) for that tracker and
proposes the eval; a human **ratifies it once**, after which it's frozen and protected.

```jsonc
{ "repo": "~/proj", "goal": "...", "direction": "max",
  "onboard": true, "onboard_command": ["python", "main.py"] }
```

```bash
looplab run my_repo_task.json --backend llm
looplab approve runs/<run>      # review the proposed eval+adapter, then ratify
looplab resume  runs/<run>
```

*Genesis:* if you state how it runs but not how it's scored, it sets `onboard` + `onboard_command`
and asks in chat how the metric is emitted.

### I only want to tune knobs, no code edits → framework mode

If the experiment already exposes hyperparameters and you just want them searched, set
`params_style: "cli_overrides"` and declare the space — proposals become `key=value` overrides on the
eval command (Hydra-style), **no code is edited**. Add cheap `smoke` / full `profiles` to keep the
search fast. See [Framework mode](tasks.md#framework-mode-tune-with-no-code-edits).

---

## Pointing at your data

Don't hard-code absolute machine paths in your scripts — **mount** the data so the run is
reproducible and works under the sandbox. A `repo` task takes two fields, addressed by a **relative
path** (relative to `eval.cwd`, default `.`):

```jsonc
{
  "dataset": { "data": "~/proj/data" },        // name → path; read-only symlink mount at ./data
  "references": [
    { "name": "libs", "path": "~/shared/lib", "mount": true }   // read-only runtime dep → ./libs
  ]
}
```

- **`dataset`** (`name → path`, legacy alias `data`): each source is a **read-only symlink mount** at
  `./<name>` by default (no per-node copy — cheap on a large/immutable input, fingerprinted without a
  multi-GB walk). Your script reads `./data/...`. A value may instead be an object with
  [per-source permissions](tasks.md#per-source-data-permissions) — `{path, mount, edit, copy_modify,
  preprocess, extend}`: `mount:false` copies it INTO the workdir (writable copy); `edit:false` (the
  default) protects the original; `copy_modify`/`preprocess`/`extend` tell the agent it may derive an
  augmented training set (defaults: everything allowed except editing the original).
- **`references`** with `"mount": true`: a read-only symlink mount for a runtime dependency; with
  `"mount": false` it's shown to the agent as **context only**, not placed in the runtime.
- `~` and `$HOME`/`$VARS` are expanded, so `"~/proj/data"` and `"$DATA_DIR"` resolve.
- Mount names must be simple subdir names (no `/`, `..`) and must not collide with each other or an
  editable repo's subdir.
- For `mlebench_real` you don't set this — the data comes from the competition prep step.

**With Genesis:** just tell it where the data is (*"data is at `~/proj/data`, mount as `dataset`"*) and
it authors the `data` mount for you. It won't invent a path you didn't give, so don't omit it — and you
can always add/adjust the `data` field on the card before launching.

---

## Passing different arguments to train vs test

> **First, clear up a common worry.** `eval.command` is the **whole command line, arguments and all** —
> you are *not* limited to a bare `["python","test.py"]`. Need ten flags? Write all ten:
> `["python","test.py","--data","./dataset","--batch-size","64", …]`. It runs exactly as written.
> "The agent can't add arguments" means the agent can't **tamper with your scoring command** (an
> anti-cheat guarantee) — it is *not* a limit on what *you* put there. The agent improves the **code**
> your command runs (inside `edit_surface`), never the command itself. Everything below is the *extra*,
> optional case where you want the **search** to *vary* some of those arguments.

The search-driven argument mechanisms attach to **`eval.command` only** — never to `eval.setup`. Two
of them:

- **`params` + a `%params%` token** — put `%params%` in the `command` (or in a declared stage's
  command) exactly where the flags belong; the Researcher's proposed hyperparameters expand there as
  `--key value`. This is a **no-code-edit** tuning mode. (The legacy `params_style: "cli_overrides"`
  still appends `key=value` Hydra-style tokens.)
- **`profiles`** — named override sets the engine picks per phase, e.g. a cheap `smoke` during search
  and a `full` on confirmation. Each profile's `overrides` are appended to `command` (and the profile
  timeout applies even in stage mode). Profile names must be non-empty, and each profile may contain
  only `overrides` (a list of argv strings) and `timeout` (a finite number greater than zero). The
  effective runtime timeout is capped at 24 hours.

So if the **agent's train stage and the scorer `cmd` need different arguments**, pick the pattern that fits:

| You want | Do this |
|---|---|
| The **search** to vary args that affect *both* train and test | Use **one entrypoint** as `command` (e.g. `["python","run.py"]`) and let it route args internally; tune them via `params`/`cli_overrides`. `setup` runs verbatim, so don't split train into `setup` if the search must tune it. |
| **Fixed** different args per phase (fast smoke vs full run) | Use `profiles` — e.g. `smoke` passes `epochs=2`, `full` passes `epochs=200`. Operator-chosen, applied to `command`. |
| The **agent** to set arbitrary, independent args for each script | Use **code-edit mode** (leave `params` empty): the agent edits a shared `config.json` / argparse defaults / a wrapper and can pass whatever it wants to train and to test — it's free-form code, not one flat override namespace. |

```jsonc
// One entrypoint the search tunes; it passes the right args to train and to test itself.
{
  "repo": "~/proj", "direction": "max",
  "params": {"lr": [1e-4, 1e-1], "epochs": [5, 50]},
  "eval": {
    "command": ["python", "run.py"],          // run.py: train(lr,epochs) → test(...), prints {"metric":…}
    "params_style": "cli_overrides",           // → run.py gets `lr=0.03 epochs=20`
    "profiles": { "smoke": {"overrides": ["epochs=2"], "timeout": 60},
                  "full":  {"overrides": [],          "timeout": 1800} }
  }
}
```

> **Why not just tune `setup`?** `params`/`cli_overrides` is a *no-edit* mode, and overrides only flow
> to `command`. If you need the agent to **write code** *and* control args, that's code-edit mode
> instead — it sets args by editing config/scripts, not via the override namespace. You can't have the
> search append tuned tokens to `setup` and have the editing agent at the same time.

### Letting the *agent* pick the arguments (you declare nothing)

A common goal: *"my `test.py` takes a dozen flags that switch between implementations — I don't want
to know or list them, the **agent** should choose and vary them."* That's exactly right, and the key
fact that makes it work is:

> **The agent emits *files*, never a command line.** It never "types" your `eval.command`; it only
> writes/edits files. So to let the agent choose arguments, the choice must live in a **file the agent
> owns**, not in the command's argv.

So keep the command **argument-free** and give the agent a tiny launcher it rewrites each iteration:

```jsonc
{
  "repo": "~/proj", "goal": "find the best config", "direction": "max",
  "edit_surface": ["run.py"],                 // the agent OWNS this launcher
  "protect": ["train.py", "test.py"],          // your real scripts: the agent calls them, can't change them
  "eval": { "command": ["python", "run.py"], "metric": {"kind": "stdout_json", "key": "metric"} }
}
```

```python
# run.py — the agent rewrites THIS every iteration, choosing the args itself.
import subprocess
subprocess.run(["python","train.py","--model","vit","--lr","0.01","--epochs","50"], check=True)
subprocess.run(["python","test.py","--model","vit"], check=True)   # prints {"metric": ...}
```

The agent **reads your repo** (it has read-only grep/list/read over the editable repo, so it sees your
argparse / `--help` and which `--model resnet|vit`, `--optimizer …` exist), then **edits `run.py`** to
try different combinations. Each node is a new combination; your protected `test.py` measures it. You
write zero arguments — the agent does, just through a file instead of the command line.

The same logic explains the [trust boundary](#how-the-commands-run-and-whats-safe): the agent can't
append flags to your *command* because the command is the anti-cheat boundary — its freedom lives in
the files it writes.

**Even simpler (the native way):** drop the CLI switching entirely — put `train.py` in `edit_surface`
and let the agent **edit the code directly** to swap implementations (ViT for ResNet, etc.). Then there
are no arguments at all: "switching" is just a code edit, which is the agent's native operator. CLI
flags were *your* fast-switch tool; the agent's is editing the code.

### Recommended: a typed config the agent edits (pydantic-settings + YAML)

The cleanest version of the launcher pattern: drive everything through a **validated config file** and
let the agent edit *that*. One fixed command loads a YAML config via
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/), validates it, runs
the experiment, and prints the metric. The agent owns the YAML; your runner and scorer stay protected.

```jsonc
{
  "repo": "~/proj", "goal": "find the best config", "direction": "max",
  "edit_surface": ["config.yaml"],            // the agent owns the config (add "src/**/*.py" to let it add new impls)
  "protect": ["run.py", "src/eval.py"],        // the runner + scorer are off-limits
  "eval": { "command": ["python", "run.py"], "metric": {"kind": "stdout_json", "key": "metric"} }
}
```

```python
# run.py — fixed (protected): load YAML → validate → run → print metric.
import json, yaml
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model: str = "resnet"          # the agent flips this to "vit", etc.
    lr: float = 1e-3
    epochs: int = 50

cfg = Settings(**yaml.safe_load(open("config.yaml")))   # bad config → ValidationError → fed to repair
print(json.dumps({"metric": run_experiment(cfg)}))
```

Why this is the best fit:

- **Validation is feedback.** An invalid config the agent wrote raises a `ValidationError` naming the
  exact field and reason; that text goes back through the **repair loop**, so the agent fixes it
  precisely instead of guessing at an opaque crash.
- **The agent sees the schema.** In code-edit mode it has read-only access to the repo, so it reads
  your `Settings` model and knows which fields/types exist — proposals are valid and informed.
- **Extensible for free.** Add a field to `Settings` (and a branch in code) and the agent starts using
  it. The config is "yours to extend however you like," exactly as intended.

Notes:

- `profiles` still compose here (they don't disable the editing agent — only `params` does). Use them
  for a cheap smoke / full split, layered on top of the YAML if `run.py` accepts CLI/env overrides via
  pydantic-settings' source precedence.
- To let the agent add **new** implementations (not just toggle existing ones), widen `edit_surface`
  to include the model code (e.g. `["config.yaml", "src/**/*.py"]`) and keep `src/eval.py` protected.

---

## How the commands run (and what's safe)

**Execution.** Every command — `eval.command`, `eval.setup`, `onboard_command` — runs as an **argv
list, executed directly with no shell**. There is no `sh -c`, so no shell injection, no `&&`/pipes/
redirection/globbing — a token like `"x; rm -rf /"` is passed as one literal argument, not
interpreted. Each run gets its **own timeout, whole-process-tree kill on timeout/cancel, and capped
stdout/stderr** (~64 KB). `setup` runs first (at the repo root); if it exits non-zero or times out the
node fails and its stderr is fed back to the agent's repair.

**Two actors — only one is trusted to name commands.** This is the core trust boundary:

- **You (the operator)** author `eval.command` / `eval.setup` / `onboard_command`. These are
  **trusted by construction** — it's your spec, your machine, your permissions. *Yes, you can put any
  argv there;* that's how your framework gets invoked. Under the default `trusted_local` tier LoopLab
  does **not** sandbox you from your own command.
- **The agent** (the LLM / coding agent) **cannot author or change** those commands, nor the file the
  metric is read from. The metric-source file (a `file_json`/`file_regex` path or an onboarding
  adapter) is auto-protected, and so is the **entrypoint** your `cmd` executes whenever the argv names
  a file the editable repo already ships (`cmd.protect_entrypoint`, default `true`) — including
  behind a transparent launcher and its own options (`chrt -f 99 python score.py`,
  `nice -n 10 python -m pkg.mod`, `srun --gres=gpu:1 …`, and `env`/`taskset`/`stdbuf`/`ionice`/
  `time`/`nohup`/`setsid`). An entrypoint the repo does *not* ship is the agent's to author by
  design, and a `cmd` that names no in-repo file at all — a shell wrapper, a console script,
  `torchrun`/`accelerate`/`deepspeed`, or a launcher spelling LoopLab will not guess at — is warned
  about at submit rather than silently unprotected. The surface gate is
  *reject-not-strip* (a patch touching anything outside `edit_surface`, or a protected/escape path like
  `..`/absolute, is rejected wholesale). So the agent's only influence on execution is the **code it
  writes inside `edit_surface`**, which runs under the sandbox tier — it can't issue an arbitrary host
  command.

**Isolation tier** is chosen by `trust_mode`, not your environment (see
[Trust & the sandbox](concepts.md#trust-the-sandbox)):

| `trust_mode` | What runs the command | Boundary |
|---|---|---|
| `trusted_local` (default) | direct subprocess | process isolation + timeout + tree-kill + output caps. **No Docker, no network/FS isolation** — it's your own code on your box. |
| `untrusted` | `docker run --rm --network none --pids-limit 1024 --cap-drop ALL --security-opt no-new-privileges --memory 4g -v workspace:/work` | no network, fork-bomb guard, all Linux capabilities dropped, no privilege escalation, memory-capped (`sandbox_memory`; optional `--cpus` via `sandbox_cpus`), only the workspace mounted; metric read from the bind mount on the host. |
| `hostile` | the above **+ gVisor** (`--runtime runsc`) | kernel-level isolation for actively hostile code. |

The same row applies to the **operator assistant's shell** (`run_command`/`run_tests`/`git`), which
is the third containerized surface of this tier and is built from the same
`sandbox.docker_tier_kwargs` translation — so raising `sandbox_memory` or setting
`sandbox_readonly_rootfs` moves the chat shell and the eval together. Under `trusted_local` the
assistant's shell runs directly on your box and makes no isolation claim at all (see the trust-boundary
note at the top of `tools/shell_tools.py`); the container is what `untrusted`/`hostile` buy.

**Other guards** on the agent's path: the [Genesis](#preferred-let-genesis-author-it) repo scout is
read-only and allow-lists file types — credential files (`.env`, keys, `~/.ssh`, …, anything
secret-shaped) are **refused and hidden**, so the LLM API key in the server env can't leak to a model.
For untrusted real tasks, **host-side grading** computes the metric from the candidate's predictions
against held-out labels the candidate can't see, so it can't self-report a score. Reward-hack,
code-leakage, and critic monitors surface in the UI **Trust panel** as audit events.

**Bottom line:** *as the operator* you can run any command you like (it's your trusted argv on your
machine). *The agent* can't — it can't run host commands, only write sandboxed code within the surface,
and can't touch the eval/scorer. If you don't fully trust the code that will run (a hosted/multi-tenant
UI, someone else's repo), switch to `untrusted`/`hostile` — see [Deployment](deployment.md).

---

## See also

- **[Tasks](tasks.md)** — the full field reference for every kind.
- **[Web UI](ui.md)** — where Genesis ("New run") and the run chat live.
- **[LLM & coding agents](llm-and-agents.md)** — choosing the model that writes the code, and
  delegating the Developer to an external coding agent (OpenCode/Aider).
- **[Concepts](concepts.md)** — sandbox & trust tiers, operators, the protect/eval trust boundary.
