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
| `kind` | string | The adapter to use (table below). **Optional** — with the composable schema it's inferred from the fields; a `kind`-less task with no recognizable capability field is rejected (no silent quadratic default). `--kind` on the CLI has no default either: omit it and Genesis picks the kind. |
| `id` | string | A short identifier for the task (groups sibling runs) |
| `goal` | string | A natural-language objective; the agent reads this |
| `direction` | `min` \| `max` | Whether lower or higher metric is better |
| `seed` | int | Random seed for reproducible data generation. **Not universal** — the built-in synthetic kinds and `repo` carry it; `mlebench_real` has none (the competition owns the split) |

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

**Each stage may declare what it READS — `needs`.** The counterpart of `expect`, and the half that was
missing until 2026-08-13: a manifest described what every stage produced and nothing about what it
consumed, so a pipeline whose stages disagreed about where a file lives had no way to say so. Both of
the most expensive failures on record are that shape — one run trained for 76 minutes and then scored a
directory the trainer never wrote to; another trained a model to recall@100 0.726 and scored a *human's*
checkpoint that an absolute path in an editable config named, with every artifact contract passing and
`0.225` recorded as the experiment's result.

```jsonc
{ "name": "score", "command": ["python", "score.py"], "timeout": 3600,
  "needs": ["ckpt/model.pt", "data/eval.parquet"] }   // workdir-relative paths this stage READS
```

Each declared input must exist inside the workdir and be non-empty **before** the stage starts. It is a
`stat()` per path, so the failure costs one second instead of the stage's runtime, and it fails *before*
the GPU-hours rather than after them. When an **earlier** stage declared the missing path in its own
`expect.files`, the refusal names that stage, so a disagreement between two declarations reads as one
sentence instead of a traceback inside somebody's loader three stages later; when a file of the same
name exists elsewhere in the workdir, that path is reported too — "your training worked and one of the
two paths is wrong" is a different repair from "nothing was produced".

There is deliberately **no freshness rule** on an input. `expect` refuses an artifact older than the
stage, because a stale output means the stage did not produce it; an input is legitimately older by any
amount — the seeded repo, a mounted dataset, a base checkpoint, or a `train` output the engine
deliberately reused across repair attempts. Applying the output rule here would refuse the very reuse
the persistent workdir exists for.

A missing input fails the stage as `needs_failed` — its own repair reason, separate from
`expect_failed`, because the stage never ran: nothing about its code is implicated, and the repair is in
one of the two *declarations*. Deleting the `needs` entry is explicitly not a fix; it only moves the
identical failure into the stage's own loader, later and more expensively. Omit `needs` entirely for a
stage that reads only the workspace it was seeded with — an empty declaration asserts nothing.

### Declaring an eval's environment

**A stage can declare what it needs SET, and so can the task and the run.** `expect` states what a
stage writes and `needs` what it reads; until 2026-08-13 nothing could say what it needs in its
*environment*, so an environment variable's only home was CODE. What that cost, measured: on
`rubertlite-dr-unified-v6` node 0 crashed on its first attempt because `VS_LOCAL_DATA_ROOT` was unset
and the data loader reached for S3 instead of the local corpus. The repair was correct and took three
minutes. Then **node 1 hit the identical error** — a node is seeded from the SOURCE repo, never from a
sibling node, so every node in the run rediscovers the same fact and spends one repair attempt on it.

Three levels, most specific wins:

```yaml
settings:
  eval_env:                                  # RUN level: every stage of every node
    VS_LOCAL_DATA_ROOT: /home/jovyan/data/dr-local
task:
  cmd:
    env:                                     # TASK level: every stage of this eval
      OMP_NUM_THREADS: "8"
    stages:
      - name: train
        command: ["python", "train.py", "%params%"]
        env: { WANDB_MODE: offline }         # STAGE level: this stage only
```

On the command line the run level is `-s eval_env=NAME=VALUE`, comma-separated for several:

```bash
looplab run task.yaml -s eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local
```

which is the same thing as exporting it in the launching shell **except that it is written down**. A
declared environment lands in `task.snapshot.json` (the two task levels) and in `config.snapshot.json`
plus the `run_started` event (the run level), so a resume reproduces the environment its earlier nodes
were evaluated under rather than re-reading whatever live config now says (engine invariant #6), and a
reader of the log can see what the nodes actually ran under. If the two disagree on a resume, the
**record wins** and the engine says so at WARNING; run a NEW run to evaluate under a different
environment, because the comparison the existing nodes belong to no longer holds. Both sandbox tiers
get it: the subprocess tier merges it into the child's environment, the Docker tier forwards it as
`-e` pairs.

Three things it refuses, all at declaration time:

* **The Developer may not declare it.** `env` is operator-only — on `cmd.stages[].env`, `cmd.env` or
  `eval_env`. An agent that could set environment for its own protected `score` stage would have
  another route around the scorer freeze (`PYTHONPATH` alone re-points every import that stage makes),
  so `declare_stages` refuses the key by name and points at the operator surface.
* **Names LoopLab owns.** `CUDA_VISIBLE_DEVICES` is the GPU pin the engine reconciles with the host
  pool lease and the container's `--gpus`; `LOOPLAB_*` is the engine's own settings namespace and
  includes `LOOPLAB_READ_FENCE_DIR`, the marker that installs the [source-tree read
  fence](generating-code.md). A declared value would silently override the run's own treatment.
* **Anything that looks like a credential** — by name (`*_TOKEN`, `*_KEY`, `*_PASSWORD`, `AUTH`,
  `COOKIE`, `WEBHOOK`, `DSN`, …) or by value (a URL carrying inline `user:password@`). This is the
  one place the refusal is about *durability rather than danger*: a declared environment is written
  verbatim into files that get exported, rendered in the UI and pasted into bug reports, and there is
  no redaction that would keep it both safe and reproducible. **LoopLab has no secret store and is not
  adding one here.** LoopLab's own credentials keep their existing boundary (`LOOPLAB_LLM_API_KEY`, a
  profile's `api_key_env` — runtime-only fields that are never snapshotted and are refused by
  `--set`); a credential your *eval's* code needs has no such boundary, because the eval sandbox is
  where agent-written code runs. Export it in the launching shell if you accept that — what this
  refuses to do is write it down.

It applies to the per-node `setup`, the single `command` and every stage. It is deliberately **not**
applied to the run-level `run_setup`, which runs once in your own source tree before any node exists.

**Each stage may declare what its success MEANS — `expect`.** Exit 0 is not evidence a stage worked: a
mining stage that produced hard negatives for 1.2% of the queries exits 0 exactly like one that produced
100%, and the next stage consumes the 1.2% as if it were whole (this happened on a real run, and the
node's whole result was meaningless). `expect` is checked by the engine after the stage exits 0 and
before the next stage runs, and has two halves:

```jsonc
{ "name": "mine", "command": ["python", "mine_hard_negs.py", "%params%"], "timeout": 14400,
  "expect": {
    "files": ["hard_negs.pkl"],                    // workdir-relative artifacts this stage WRITES
    "assert": "hard negatives for at least 90% of the training queries"   // what success MEANS
  }}
```

* **`files`** — technical and deterministic, no LLM: each declared path must exist inside the workdir,
  be non-empty (a directory must be non-empty too), and have been written by **this** run of the stage.
  The freshness rung is the one that matters on a repaired node: the workdir deliberately persists
  across repair attempts so a completed `train` can be reused, which is exactly what makes a leftover
  artifact plausible — a stage that "succeeds" without rewriting its output hands the next stage a
  previous attempt's file.
* **`assert`** — one line stating the condition, in the declarer's own words, checked against what the
  stage **prints** by the inter-stage checker. Declaring it opts that stage's check in (`check: true` is
  not additionally required) and is the ONLY thing that unlocks the checker's
  `declared_condition_violated` verdict — without a declaration that verdict is refused and degrades to
  `inconclusive`, because a checker may not invoke a contract that does not exist. State the
  **work**, never the result quality — `"recall beats 0.85"` is the search's judgement, not a stage's,
  and the checker will not enforce it.

**The metric must say what it is ABOUT — `eval.metric.subject`.** `expect` and `needs` describe a
stage's files; `subject` describes the *number*. It names the workdir-relative artifact the metric is a
claim about, and the engine binds the recorded number to that artifact's content identity at the score
stage's start:

```jsonc
"eval": {
  "cmd": ["python", "score.py"],
  "metric": { "kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)",
              "subject": ["outputs/final/model.safetensors"] }
}
```

`node_evaluated.metric_provenance` then carries, per subject, the `(dev, ino, size, mtime_ns, ctime_ns)`
identity tuple, a sha256 (full under 256 MiB, **sampled** above it — the mode is on the record), and the
stage whose `expect` promised the path. It is the operator's field, on the operator's protected stage,
for the same reason scoring is: the agent writes the training script and therefore writes the very text
an extractor would read.

Why it exists, in numbers: across the six repo runs that have an event log, **82 of 83 recorded metrics
carry no provenance at all** — the one exception is the salvage path, i.e. provenance is written only
once something has already failed — and **2 of 83 are provably about bytes the node did not produce**.
Those two are the same number, `0.224975`, recorded by two independently authored nodes three weeks
apart from the same foreign checkpoint. The node's own checkpoint and the foreign one are byte-identical
in *size* (92,174,712), so `exists` / `non-empty` / `fresh` — every predicate the artifact contract owns
— are satisfied by both. Only content or inode identity separates them.

What binding does and does not buy, stated plainly:

* it gives the number a **referent**, so a replayed run can answer "what is this about?" — which,
  before this, no run could;
* under `metric_subject: "require"` an **unbound** metric (no subject declared, or one that is missing,
  empty, stale, or escapes the workdir) carries the existing `metric_salvaged` violation, so the node is
  counted and visible and is never selectable;
* `stale` means *this attempt* did not produce it — a leftover from an earlier repair attempt in the
  workdir every attempt reuses. It is **not** applied when the engine ITSELF reused the earlier stages:
  on a stage-scoped re-run (a repair that only touched the scorer, so `train` is skipped and its
  checkpoint kept) the earlier attempt's artifact is the subject on purpose, and the freshness floor is
  dropped for exactly that attempt — the same floor the constraint / extra-metric / cross-check readers
  already use, derived once in `runtime/command_eval.py::attempt_freshness_floor`. The primary metric
  read keeps its floor either way: it comes from the stage that *did* re-run;
* it does **not** prove the score stage read the subject. Neither does `needs` — measured against the
  preserved node-4 workdir, `verify_stage_inputs` with a perfectly correct `needs` naming the node's own
  checkpoint returns "no problem", because the node really did write that file and the scorer read
  somewhere else anyway. `needs` is a *presence* check and, **under `metric_subject: "require"` only**,
  the engine derives the protected `score` stage's `needs` from `subject`, which buys **latency** (the
  refusal fires before the scorer runs) and not coverage. It is a `require` effect and not an `audit`
  one because the rungs are ordered: `audit` is what you turn on to find out whether `require` is
  affordable, so it records and never gates. What makes "read elsewhere" impossible is the read boundary
  — `read_fence` (on by default) and, opt-in, the kernel allow-list `landlock`.

When that derived contract does fail, the refusal says whose failure it is. The `needs` on the `score`
stage is written by the engine, not by the Developer, and the Developer may edit neither that stage nor
`eval.metric.subject` — so the message names the one repair it *does* own (make the pipeline produce the
path) and names the operator for the other (the declaration itself is wrong). Repair eligibility is
deliberately kept: the artifact is produced by the agent's own pipeline, unlike a missing **protected
script**, which no edit can ever create and which the engine refuses without asking for a repair.

A failed `expect` fails the stage the same way a non-zero exit does (same `failed_stage`, same repair
path), so nothing new happens mid-loop — but see **metric salvage** below: a stage that failed its
`files` contract *after* already computing the metric no longer loses it. `expect` lives in the
**manifest**, not in the script, which is
what makes it usable in both repo-task modes: the scorer is usually authored by the Developer, but when
an operator `protect`s it the agent cannot add an assert to it at all — and then the manifest is the only
place that stage's success condition can be stated. (An in-script `assert` is still the recommended belt
where the agent owns the file; the two are not redundant.)

**Metric salvage — a failed node does not have to lose a metric it already computed.** A real run
(`rubertlite-dr-unified-v5` node 0) trained for 76 minutes on two H200s, exited 0, ran its scorer and
printed `RECALL@100: 0.743250` — exactly what the run's declared reader matches — and was then failed
with `reason: no_metric` because its manifest declared the checkpoint one directory name away from
where the testbed wrote it. Since 2026-08-12 the engine asks the **operator's own declared metric
reader** one more time, over that failed attempt's own stdout and workdir, before writing the node's
single terminal event. If it finds a value, the node terminalizes as `node_evaluated` carrying it.

What makes a salvaged metric admissible is narrow on purpose:

* it comes out of `eval.metric` — the operator's spec, unchanged. Never an agent-authored one, and
  never `kind: "adapter"` (that reader EXECs agent code, so salvage refuses it outright). **No model
  reads the number**: an LLM extractor would let the agent, who writes the training script, write the
  text the extractor reads, which is a scoring path around the protected `score` stage;
* file readers are held to the same freshness gate as the primary read (this attempt's own output),
  and a `file_json`/`file_regex` reader whose declared path is wrong is retried at the fresh
  same-basename file the stage actually wrote — the same near-miss rule `expect.files` reports;
* the failure must not be one of *measurement* or *trust*. A drift rejection, a hard timeout, a setup
  failure, a non-zero exit (except the authenticated stall verdict) and a failed `expect.assert` /
  `check` are all refused. A stage that genuinely produced nothing has no fresh value to find, so the
  reader itself is the discriminator;
* the terminal records `metric_provenance` (which rung, which reader, which stage, **who wrote the
  output it read**, the failure it overrode verbatim, and whether the cause was then repaired), and
  under the default policy the node also carries a `metric_salvaged` violation — so it is
  **evaluated and counted** (budget, UI, digest, lineage) but is not `feasible`, i.e. it cannot be
  the reported champion or be bred from. A salvaged metric is never silently equal to a measured one.
  Set the engine's `metric_salvage` to `"select"` to accept salvaged metrics as selectable, or
  `"off"` for the pre-2026-08-12 behaviour;
* **`select` applies only to output the operator's own pipeline produced.** The spec is always the
  operator's; the OUTPUT is whatever the failing stage printed, and in Developer-manifest mode that
  is a script the agent wrote — so a Developer whose training script prints `RECALL@100: 0.999` would
  otherwise have it admitted every time one of its own stages failed a contract. The provenance
  records `producer: operator_stage | agent_stage` (the protected `score` stage, an operator
  `cmd.stages` entry and the single-command eval are the operator's), and agent-produced output keeps
  the `metric_salvaged` violation under `select` too. It is still recovered, recorded and counted —
  it just does not compete;
* **the operator's constraints, extra readers and drift cross-check run on it**, exactly as they run
  on a measured metric. A salvaged node that breaches a bound is infeasible for that bound, and a
  salvaged value its cross-reader cannot corroborate is refused outright (the node fails and the
  divergence is recorded as `spec_drift`) — re-admitting a metric the drift gate would have discarded
  is the one thing salvage must never do;
* **the cause is repaired in the same breath.** The Developer is asked to fix the declaration that
  broke — committed through the ordinary `node_repaired` event, with `triage_action:
  "salvage_cause_fix"` — and the node terminalizes on the metric it already has, without paying for a
  second evaluation. A salvaged node does not carry a broken manifest into its next attempt.

**The exclusion covers cross-run memory too, not just this run's champion** (since 2026-08-13).
Being out of `feasible_nodes` stops the node winning *here*; it did not stop the run writing what
that number "showed" into the **shared** store, which is where a later run reads it as evidence. So
an excluded salvaged node grounds no cross-run claim about its metric: it is not one half of a
comparative (credit-assigned) pair lesson, not a row of the ranked table the whole-run reflection
generalizes from, and not evidence on an auto-distilled skill card. The same rule covers a node the
run's own trust gate excludes under `trust_gate: gate | block` — a reward-hack or leakage flag makes
the number one nobody earned, which is the same defect one step over. A node that breached one of
your **constraints** is unaffected: its number was measured and its exclusion is a fact about the
bound, so it keeps its place everywhere.

What the node *observed* is still recorded, because that half is independent of the unmeasured
number: its concepts go into the run's concept capsule (the tag, never a numeric outcome), and the
run-end reflection is shown the node as an **observation** — its salvage condition and the failure it
overrode, with no metric attached — so "this declaration shape breaks" can still become a lesson.
Under `metric_salvage: "select"` with operator-produced output no violation is minted at all, and the
node stays in every population, annotated as recovered rather than measured.

**And when such a node becomes the champion, the PORTFOLIO says so** (since 2026-08-15). The two rules
above are exclusions, and an exclusion has nothing to say about a number the run selected on: a
`select`-admitted salvage is in `feasible_nodes`, so it can be `best_node_id`, so it can be the
`best_metric` every cross-run surface reads off `/api/runs` — a row that carries the number and no
violations, no provenance and no node id, i.e. one no client can qualify. `best_metric_caveats` is that
missing fact: `salvaged` when the champion's number was recovered and admitted, `trust_flagged` when the
champion carries a hard reward-hack/leakage signal a `trust_gate: audit` run enforced nothing about. It
is the complementary half of the same two families the cross-run exclusion joins — salvaged-and-admitted
rather than salvaged-and-excluded, flagged-and-not-enforced rather than flagged-and-enforced — and it is
derived from those same two predicates, never re-read off the rows (`engine/champion_caveats.py`).
Measured over the 46 preserved runs when it shipped: 37 carry a best metric and none of them is
caveated, so this fences a reachable state rather than describing the corpus.

**A repaired declaration whose contract then passes is not a salvage at all** (since 2026-08-13).
When the failure was an artifact contract and the cause fix corrected the manifest, the engine
re-asks the **artifact check** — never the stage, which is the whole economy of salvaging — against
the corrected declaration. If it passes, the contract is satisfied by the artifact the pipeline
really produced: the node is recorded as **measured**, with no `metric_salvaged` violation and no
salvage provenance, so it competes for champion and can be bred from. Nothing about the number was
ever in doubt, only the sentence describing where it lived.

Four rules bound it, and each fails closed:

* **the whole declared pipeline must have run**, with the contract failure on its LAST stage. A
  contract failure aborts the pipeline, so a node whose `train` stage failed never ran the stages
  after it — including the operator's appended `score`. Correcting the path proves where one stage's
  output was; it says nothing about stages that never executed, and promoting on it would file a
  train-only number under a node claiming to be a whole pipeline. (`rubertlite-dr-unified-v6` node 3
  is exactly that shape and stays salvaged: it declared `train` → `merge`, the contract failed on
  `train`, and `merge` and `score` never ran.);
* the re-check keeps `since` at **the stage's own start** — the exact floor the original check used,
  recorded on the stage row — so a leftover from an earlier attempt in the deliberately-reused
  workdir cannot satisfy the corrected path. No recorded floor means no re-check;
* **only a repair whose changed set is exactly `looplab_stages.json` qualifies.** A repair that
  touched code changed what the stage would produce, so its artifact must be re-*run*, not
  re-checked; a declared artifact that is itself a file the repair wrote is refused for the same
  reason;
* only an `artifact_contract` failure whose value came out of the operator's spec unmodified is
  promoted (a relocated-file recovery means a second declaration is still wrong).

**Today no live node satisfies all four**, and that is worth knowing rather than discovering: the
first rule needs the failure on the pipeline's final stage, and in Developer-manifest mode that stage
is the appended `score`, which carries no `expect` at all — while in operator `cmd.stages` mode the
declaration lives in the task spec, which `looplab_stages.json` cannot correct. So a salvaged node
stays salvaged until someone decides whether the appended `score` stage may carry an operator-declared
`expect`, and who may repair it.

The node still records `metric_provenance` — `salvaged: false`, `declaration_repaired: true`, the
stage, the reader, `producer`, the corrected `expect_files` and the contract failure verbatim. It
carries **no violation**: the record exists because "the manifest was wrong and we fixed it" is worth
knowing even when the number is sound, and it is the only durable trace that the node's recorded code
is not byte-for-byte what produced its recorded metric. Note what the re-check does *not* prove: the
failing stage is usually the Developer's, so the protected `score` stage never ran — the check says
the stage produced what it declared, not that the operator's scorer ran, which is why `producer` stays
on the record.

**What the agent may EDIT is a separate, independent decision** — `edit_surface` (globs the agent may
edit; default = the whole repo) minus `protect` (exceptions).

**The file your `cmd` runs is protected by default, when your repo already ships it.** If `cmd` is
`["python","-m","pkg.mod"]` or `["python","eval/score.py"]` and that file exists in the editable
source, LoopLab adds it to that repo's `protect` for you: the write gate refuses it and it is
materialized into every node workdir like any other protected file. That is what the Developer's own
prompt has always claimed ("the operator's `cmd` is appended as the final, protected `score` stage —
you cannot rewrite how the run is scored"), and until 2026-08-12 only the *stage* was protected while
the *file* sat in the edit surface. On a live run with `protect: []` and `edit_surface: ["**/*"]` the
Developer edited the scorer to shell out to the training module when it found no checkpoint — the
run paid twice for GPU on every node and reported a number its own train stage had not produced.

Three things follow, and each is deliberate:

* **A scorer your repo does *not* ship stays the agent's to author.** That is the normal flow for this
  adapter — you name `cmd: ["python","looplab_eval.py"]`, the Developer writes it. Protection keys on
  *the file exists in the source*, precisely so this case is untouched.
* **The opt-out is `cmd.protect_entrypoint: false`**, not "list your own `protect`". `protect` only
  adds, so keying the off-switch on it would mean protecting `data/**` silently gave up the scorer
  freeze. Use it when your repo happens to ship a file at the scorer's path but you want the agent to
  rewrite it anyway.
* **A `cmd` that names no in-repo file warns at submit** — `["bash","run.sh"]`, a console script,
  `python -c`. Nothing there can be protected, so `looplab run` and `/api/start` say so while you can
  still add the real files to `protect`. It is a warning, not a refusal: such a command is legitimate.
* **A transparent launcher in front of the interpreter is read through**, with its own options:
  `chrt -f 99 python score.py`, `nice -n 10 python -m pkg.mod`, `env -u PYTHONPATH python …`,
  `srun --gres=gpu:1 python …`, and the same for `taskset`, `stdbuf`, `ionice`, `time`, `nohup`,
  `setsid`. Those ten programs exec what follows them, so the argv is still
  `<interpreter> [flags] <target>` from the interpreter token on. A launcher whose *own* grammar
  decides which token is the script — `torchrun`, `accelerate launch`, `deepspeed` — is **not** read
  through and warns at submit instead, because a wrong guess would freeze a file you never named.
  The same warning covers a launcher spelling LoopLab cannot resolve rather than assuming an arity
  nobody verified: `srun --gres gpu:1 …` (the *separated* form — Slurm's option set is large and
  version-dependent, so only the self-contained `--opt=value` spelling is read through),
  `ionice -p PID` and `chrt -p PID` (which run no command at all), and `env -S "…"` (which hides the
  whole command inside one token). If your `cmd` is warned about and the scorer must be frozen, name
  it in `protect`.

**What is protected is the entrypoint file, not what it imports.** A scorer's import closure is the
repo (its model, config and data modules), so freezing it would freeze everything and leave the agent
nothing to change. A scorer that reads its checkpoint path from an *editable* config can therefore
still be pointed elsewhere; what holds that line is the **source-tree read fence**, not a wider
`protect` list and not the stage `expect` contract.

**Before any of that: a path that contradicts the node's own manifest is refused as it is written.**
The fence is a *read-side* mechanism and it fires when a training process follows a bad path, hours
after the path was authored. One shape of bad path is decidable from two things the Developer itself
wrote, at the moment it writes the second one: an **absolute path into the editable source tree that
names a file the node's own stage manifest declares its pipeline WRITES**. Those two cannot both be
true — a node evaluates in its own materialized copy, so the source tree can never hold anything that
node produced — so `write_file`, `edit_file` and `declare_stages` refuse it and say which two
declarations disagree. The model fixes it for the price of one tool call, before a GPU is reserved.

It is deliberately **not** a ban on absolute source paths, and the difference is measured. Over every
authored working set in this project's `runs/` corpus (2,577 of them), a blanket ban refuses 8 nodes
and 5 of those are legitimate — a committed base model, a teacher checkpoint used for distillation,
a config naming a *different* experiment. The collision rule refuses 3, and they are exactly the 3
nodes that produced a wrong or wasted result. The property that buys that: **a legitimate input is
never a path the node declares it produces.** So if you genuinely need to read a large in-tree file
the seeding does not copy, the answer is the same as everywhere else — declare a `data:` or
`references:` mount, or `seed_mode: "all"` — and the refusal message says so.

The operator's own side of this is a **warning, not a refusal**: an `eval.command` or
`cmd.stages[].command` argv token naming the editable root absolutely is reported at submit
(`looplab run`, `/api/start`), because it reaches your original tree rather than the node's copy, so
every node reads the same bytes and no node's edits to it take effect. It stays a warning because
there is no manifest to collide it against at submit time and a fixed input is legitimate. An
absolute `eval.cwd` needs no rule at all — it is remapped onto the node workdir.

**The source-tree read fence (`read_fence`, default `deny`).** A node runs in its own copy of your
editable repo, so the source tree provably cannot contain anything that node's pipeline produced.
The fence makes reading it impossible: a generated `sitecustomize.py` under
`<run>/.looplab-fence/` goes first on the eval's `PYTHONPATH` and installs an audit hook that refuses
any `open` resolving under an editable source root, raising in the child with a message that names
the fix — so it lands in the node's own stderr and reaches the repair loop. It exists because
`expect` checks what a stage **writes** and never what it **reads**: on `rubertlite-dr-unified-v6`
node 4 trained a good model (its own `train.log` records `RECALL@100: 0.726`) and then scored a
*human's* checkpoint that an absolute path in an editable config pointed at (`score.log:
0.225` — the number the run recorded), with the artifact contract passed and no violation anywhere.

**It also refuses to CHANGE the tree.** `open` is not the only way to touch a file: `os.remove`,
`os.rename`, `os.truncate`, `os.chmod` and their family raise their own audit events and none of them
raises `open`, so until 2026-08-13 a node's eval code could delete or rename your editable tree while
every read of it was refused — `shutil.rmtree` of the source root included. The same twelve events are
now refused, with a message that says so. `warn` still lets them through and logs them.

What stays readable — and writable: the node workdir, the run directory, `/tmp`, site-packages, the
model/HF cache, and every `dataset`/`data`/`references` mount **source** — a mount is the sanctioned
read channel and is allow-listed even when it lives inside the editable tree. The fence is a no-op for
a non-repo task and for the Docker tiers (the source is never bind-mounted into a container).

**What it cannot see, and you should know before relying on it.** It is a CPython audit hook, so it
covers what goes through CPython and nothing else. A native reader — `safetensors`, `h5py`, `pyarrow`,
anything calling libc directly — reads straight through it, and `safetensors` is the loader for the
exact file type the incident above was about. So does a non-Python child (`subprocess.run(["cat", …])`,
or a stage command that is not python), a child started with `python -S`/`-E`/`-I`, and a read through
a symlink or hardlink planted in the workdir. Closing those needs a kernel boundary rather than a
Python hook; the options, and what each costs, are in
[the coverage audit](../38-fence-coverage-audit-2026-08-13.md).

The one legitimate refusal is a large **untracked** in-tree input that `seed_mode: auto` does not
copy into the workdir. Fix it by declaring a `dataset`/`references` mount or setting
`seed_mode: "all"` — the refusal names all three. `read_fence: "warn"` lets the read through while
logging it to stderr and to `<run>/.looplab-fence/violations.log`, which is the honest setting for
one run while you find out what your pipeline actually reads; `"off"` installs nothing.

**A `protect`ed file is also always MATERIALIZED into every node workdir**, whatever `seed_mode` says —
it is copied from the editable source after the tree seed, before the mounts. That is not cosmetic: the
default `seed_mode: auto` copies git-**tracked** files only, so an operator scorer that was never
committed (the usual state of a file added to drive LoopLab) used to be simply absent from the workdir
the protected `score` stage runs in, and the node died with `python: can't open file
'<workdir>/looplab_eval.py'` *after* paying for the whole train. `protect` governs writes; it now
governs seeding too, so the two halves of "the operator owns this file" agree. A `protect` entry
matching nothing in the source is still fine (protecting a file the eval *creates* is legal) — but if
the eval command then tries to **run** a protected script that is not there, the node fails
immediately, before the pipeline starts, with a message addressed to you: the agent cannot repair it,
because a protected path is exactly what the write gate refuses. Commit the file, set `seed_mode:
"all"`, or drop it from `protect` so the Developer may author it.

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
- A mount name that collides with a top-level entry of the **root editable** repo is rejected when the
  workspace is seeded. The repo is materialized at the workspace root first, so the mount's destination
  would already be occupied and the eval would silently read the repo's copy instead of your declared
  source. Only a real collision fires: the check runs against what was actually seeded, so a
  git-ignored directory (under the default `seed_mode: auto`, which copies tracked files only) and a
  context-only `references` entry (`mount: false`) never trigger it. Rename the mount or the repo entry.
- A metric/prediction source file above **256 MiB** is refused and reads as "no metric" (the node fails)
  rather than being buffered whole into engine RAM. Emit a compact metric line or a summary file instead
  of a giant `predictions.json` if you are near that bound.
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
| [`classification`](#classification) | Pick a feature map + tune a classifier | K-fold CV (built-in) | `examples/classification_task.json` |
| [`timeseries`](#timeseries) | Tune a forecaster | Backtest (built-in) | `examples/timeseries_task.json` |
| [`code_regression`](#code_regression) | **Write the code** | CV printed by the solution | `examples/code_regression_task.json` |
| [`mlebench`](#mlebench) | Beat a private grader | Held-out grader | `examples/mlebench_task.json` |
| [`mlebench_real`](#mlebench_real) | **Real Kaggle competition** | Official grader | `examples/mlebench_real_spooky.json` |
| [`repo`](#repo) | Edit an existing repo | The repo's **own** eval | `examples/repo_task.json` |
| [`dataset`](#dataset) | **Write the whole solution** on your data | Self-reported (agent-chosen) metric | `examples/dataset_task.json` |

---

## `quadratic`

A toy numeric objective, and the one kind that runs with no model at all — pass `--backend toy`
(`backend` itself defaults to `llm` since 2026-08-04). The Researcher proposes points; there's no code
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

Choose a polynomial feature-map `degree` — plus the learner's `lr`/`l2`/`iters` — to maximize K-fold
CV accuracy on two **concentric rings**. The classes differ only in their distance from the origin,
so the true boundary is a circle and a straight line is the wrong hypothesis class: a `degree` 1
learner never exceeds **0.555** anywhere in the advertised `lr`/`l2`/`iters` range, while `degree` ≥ 2
reaches **0.905**. That gap is the gradient the search climbs.

```jsonc
{
  "benchmark": "classification", "id": "ring_classification",
  "goal": "choose a polynomial feature-map degree + the learner's lr/l2/iters to maximize K-fold CV accuracy on two concentric rings",
  "direction": "max",
  "n": 200, "gap": 1.6, "noise": 0.6, "seed": 0, "cv_k": 5, "max_degree": 4
}
```

| Field | Description |
|---|---|
| `n` | Number of samples |
| `gap` | Radial distance between the two rings (lower = harder) |
| `noise` | Gaussian smear on each ring's radius (higher = harder) |
| `max_degree` | Largest feature-map degree the search may try |
| `cv_k` | Cross-validation folds |

!!! note "Why this example changed (2026-08-05)"
    It used to generate two linearly-separable Gaussian **blobs** and template only `lr`/`l2`/`iters`.
    That objective was flat — 163 of 175 grid points scored the identical `0.925` — so every node
    tied and the champion was arbitrary. Worse, the one idea the Researcher kept proposing on this
    task (*expand the features to degree 2*) was not a template parameter, so it was silently dropped
    and the emitted code stayed plain-linear under a rationale describing the expansion. Both halves
    moved together: the data now *needs* the expansion and the Developer can *build* it. The task id
    changed from `blob_classification` to `ring_classification` so cross-run
    [claims](memory.md) are not pooled across two different datasets.

    A templated Developer can only ever build its own parameterization, so `llm_roles` also states
    the build surface in the prompt ("the Developer fills a fixed template from exactly those four
    numbers"). For genuinely open-ended structure use an LLM Developer that writes the code
    ([`code_regression`](#code_regression)), not a wider template.

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
protected so the agent can't overwrite it. Setting `host_graded: true` moves scoring out of the
candidate process entirely (it writes `predictions.json`; the host scores it and no answer key is
written into the workdir).

> **This is a test fixture, not a confidentiality boundary.** The blobs are a pure function of
> `seed`, so a determined candidate can recover the held-out labels from the `test.json` it was
> given by searching seeds — no answer key needed. Use it to exercise the held-out-grading pipeline
> offline; for a benchmark where the key genuinely cannot be derived, use
> [`mlebench_real`](#mlebench_real).

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
| `protect` | Files the agent may **never** touch (e.g. the eval entrypoint). Also copied into every node workdir regardless of `seed_mode` — see the note above |
| `eval.command` | The command run to evaluate a candidate (**argv list, no shell** — no `&&`) |
| `eval.protect_entrypoint` | Freeze the file `eval.command` executes, when the argv names one (`python -m pkg.mod`, `python score.py`, optionally behind a transparent launcher such as `chrt -f 99 …` / `nice -n 10 …` / `srun --gres=gpu:1 …`) **and the editable source already ships it**. Default `true`. Set `false` to hand a shipped scorer back to the Developer. A command naming no in-repo file (a shell wrapper, a console script, `python -c`, `torchrun`/`accelerate`/`deepspeed`) is warned about at submit instead — see the edit-surface section above |
| `eval.setup` | Optional command run **before** each eval to install **dependencies** (e.g. `pip install -r requirements.txt`). **Not for training** — training is a stage the agent declares (see below). |
| `eval.metric.reader` | How to read the metric: `stdout_json` / `stdout_regex` / `file_json` / `file_regex` / `auto`. Legacy `eval.metric.kind` still works **for the four concrete readers only** — `"auto"` must be spelled `{"reader": "auto"}`, because only that spelling folds to the onboarding path (`adapters/tasks.py:241`); `{"kind": "auto"}` is not a known reader and raises. |
| `eval.metric.key` | The **JSON key** to read (`stdout_json`, `file_json` — dotted keys supported) or the **regex** (`stdout_regex`, `file_regex`). For the two `file_*` readers this is the key/pattern *inside* the file, **not** the file itself — the file is `eval.metric.path`. |
| `eval.metric.path` | **`file_json` / `file_regex` only** — the metrics file the candidate/framework writes, relative to the eval workdir. Required by those two readers; ignored by the `stdout_*` readers. The same rule applies to **every** reader slot below (`eval.metrics`, `eval.constraints`, `eval.cross_check`): a `file_*` reader without a string `path` is refused at submit, because it can never read anything and each slot then fails a different silent way. An existing run whose snapshot predates that check still resumes — `looplab resume` prints the refusal as a warning instead. |
| `eval.metric.resource_key` | Optional JSON key for an explicit training resource (for example `step`). ASHA live kill is supported only by `stdout_json` and compares observations carrying the same declared resource value; without it endpoint ranking is advisory only. That comparison is the evidence, not the decision: the stop itself requires a confident `stop` verdict from the ASHA judge (`asha_live_kill_confidence`), so a run with no LLM client is never killed this way. `stdout_regex` supports advisory ranking but never kill; the other readers have no live-watchdog path. |
| `eval.timeout` | Per-eval timeout (seconds) — set it generously for training (often 7200–14400) |
| `data` / `dataset` | `name → path` map, **read-only symlink-mounted** at `./name` by default; a value may be a [per-source permission object](#per-source-data-permissions). `~`/`$VARS` expand |
| `references` | Read-only inputs: `[{name, path, mount}]` — `mount: true` exposes the source at `./name` as a **read-only symlink** (and a read-only bind mount under the Docker tiers), **not** a copy (`engine/workspace.py:183-185`, `engine/eval_dispatch.py:151-153`); `false` is context-only. Edits under `./name` therefore reach your source — the read-only mount is what prevents that. |
| `editables` | Multi-repo workspace: extra editable repos, each mounted at its own `name/` subdir |
| `eval.stages` | Operator-declared ordered pipeline (`data_prep` → `train` → …). When set, these **are** the canonical stages and the Developer's own `looplab_stages.json` is ignored; the LAST stage's stdout carries the metric. Each stage is `{name, command:[argv], timeout?, check?, needs?, expect?, env?}` — `needs` lists the workdir-relative files the stage READS (checked before it starts) and `expect` is the stage's success contract (`{files?, assert?}`, see above); declaring either on an operator stage is how you hold a stage the agent may not edit to a condition. `env` is the stage's declared environment and is OPERATOR-ONLY (see [Declaring an eval's environment](#declaring-an-evals-environment)) |
| `eval.env` | The DECLARED ENVIRONMENT for this task's eval: `{NAME: value}` applied to `setup`, the single `command` and EVERY stage, with a stage's own `env` overlaying it. This is where a fact about the **repo** belongs — "the local corpus lives at …" is true of the repo, not of the engine — and stating it once means every node inherits it instead of each one rediscovering it by crashing. It rides in `task.snapshot.json`, so a resume re-applies the same environment the results were produced under. Operator-only, and a secret-shaped name or value is refused: see [Declaring an eval's environment](#declaring-an-evals-environment) |
| `eval.cwd` | Working directory for the eval, relative to the node eval workdir (default `.`) |
| `eval.setup_timeout` | Per-node `eval.setup` budget in seconds (default `600`) |
| `eval.run_setup` | **Run-level** setup: runs ONCE at run start in the editable repo root, not per node — the autonomy default when deps don't change between experiments. A failure aborts the run. **You usually do not need to set this.** When your first editable repo ships a `requirements.txt`, LoopLab derives `python -m pip install -r requirements.txt` and runs it here by default, so the repo's pinned versions are the versions the eval gets (`auto_install_deps`, on by default, `trusted_local` only). Setting it explicitly **replaces** that default entirely — nothing is prepended or merged — which is how you install something else, or nothing |
| `eval.run_setup_timeout` | `run_setup` budget in seconds (default `1800`) |
| `eval.profiles` | Named override+timeout sets the Researcher may pick per node, e.g. `{"smoke": {"overrides": ["max_steps=20"], "timeout": 60}}`. Names must be non-empty; each profile is a closed object with only `overrides` (a list of argv strings) and optional `timeout` (a finite number greater than zero). Runtime caps any single eval at 24 hours, and the LLM hint reports that effective capped timeout. The LLM Researcher is shown only the exact names declared by this task; the offline parameter Researcher selects `smoke` only when it is actually declared. Omitting `eval_profile` uses `smoke` when declared, otherwise the base eval; an unknown explicit name also uses the base eval and is never silently mapped to a cheaper profile. The confirm phase requests `full`; when no `full` profile exists, that likewise means the base eval. |
| `eval.params_style` | `none` (default) or `cli_overrides` |
| `eval.metrics` | Extra **named** readers reported alongside the primary, for audit/observability: `{"latency_ms": {"kind": "stdout_json", "key": "latency"}}`. A `file_*` reader here needs its own `path` — without one it is silently dropped and the node just reports no value under that name. **This is not the only way a value reaches `extra_metrics`, and the difference is now on the record.** Every OTHER numeric key on the primary metric's own stdout JSON line is AUTO-CAPTURED too — no declaration, no reader spec, no `adapter` refusal — which is how all 1,642 secondary metrics in this box's preserved runs got there, 1,636 of them the four keys of a CUDA probe (one a schema VERSION number). `node_evaluated` now carries `extra_metrics_provenance` (`{name: "declared"|"auto"|"engine"}`) beside the values, and every surface that shows a secondary metric says which channel it came through; a value with no tag is from a run recorded before 2026-08-14 and reads `unknown`, never `declared`. `engine` names a key the engine's OWN spliced instrumentation declared and its source authenticates — trustworthy, and still a diagnostic rather than a result. Set `auto_extra_metrics: false` to record only what YOUR readers produced (plus that engine instrumentation, which was never the candidate's) |
| `eval.constraints` | Reader specs carrying a `max`/`min` bound. A node that violates any (or whose constraint value can't be read) is still measured but **excluded from best-selection** — "optimize the metric subject to `latency_ms <= 100`". Operator-owned (trust boundary). A `file_*` reader here needs its own `path`: an unverifiable constraint counts as a violation, so a pathless one excludes *every* node |
| `eval.cross_check` | An INDEPENDENT built-in reader (`stdout_json`/`stdout_regex`/`file_json`/`file_regex` — never `adapter`) that re-reads the same metric from a source the agent can't forge. Used by `eval_trust_mode="ratify_freeze_drift"`; `None` disables it. A `file_*` reader here needs its own `path`: the drift check fails closed, so a pathless one discards *every* node's metric |
| `eval.drift_tolerance` | Tolerance for the `cross_check` comparison (default `1e-6`; must be finite and ≥ 0) |

The metric-source file and the files you list in `protect` cannot be overwritten by the agent
(enforced by the write/diff gate), and so is the scorer entrypoint itself when `cmd` names a file the
repo already ships (`cmd.protect_entrypoint`, on by default — see the edit-surface section above). Offline or
on agent failure, a no-op developer leaves the repo unmodified.

> **Have a test/eval but no training script?** Set `cmd` to the scorer (`["python","test.py"]`); it is
> protected automatically because the repo ships it — the Developer declares a `train` **stage** in its dedicated STAGES phase (the first of
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
