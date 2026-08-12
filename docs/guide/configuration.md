# Configuration

LoopLab is configured by a single layered `Settings` object (`looplab/core/config.py`). Every field can
be set four ways, in increasing priority:

1. **Default** (shown below).
2. **Environment variable** — uppercase the field name and prefix with `LOOPLAB_`
   (e.g. `max_nodes` → `LOOPLAB_MAX_NODES`). A `.env` file in the working directory is read too.
3. **A config file** — the `settings:` block of a unified YAML/JSON file passed to `looplab run`
   (see below). `looplab init` scaffolds a documented template whose common settings are active and
   whose long-form appendix is commented out; those active values override matching env vars.
4. **CLI flag** — a named flag for the common knobs, **or** `-s/--set key=value` (repeatable) for
   **any** setting by its exact field name.

The resolved, **secret-masked launch settings** are written to `config.snapshot.json` in every run
dir. The file carries its own format version in `config_snapshot_schema`; a snapshot written by a
**newer** LoopLab is refused rather than loaded, because `Settings` ignores fields it does not
recognize and resuming would otherwise continue the same event history under different paid,
concurrency or selection semantics with no diagnostic. Upgrade LoopLab to resume such a run. A
snapshot with no version key predates the marker and still loads under its historical contract: a
pre-versioned snapshot is a full settings dump, so a **missing** key means the field did not exist
when the run was launched, and resume restores the pre-field behaviour (feature off, cadence `0`)
instead of today's default. That keeps re-entry from adding paid calls, interventions, concurrency
or a different selection policy to a run that never had them — see
`LEGACY_CONFIG_SNAPSHOT_DEFAULTS` in `looplab/core/config.py` for the exact list and for the two
classes (knobs latent under an off parent, and magnitudes for behaviour that already existed) that
are deliberately left out.

`resume` loads that snapshot, but it is not the sole authority for every effective field:
`card_driven_selection`, `speculation_depth`, `holdout_fraction`, `holdout_select`, `select_verifier`,
`select_verifier_samples`, and `verifier_ci_tie` are committed by `run_started` and restored from
the folded event log. A later
`trust_gate_changed` event likewise owns the effective trust gate. The owner per-run config API
overlays those folded values and marks the seven run-start fields read-only; `looplab inspect`
deliberately prints the raw on-disk launch snapshot for diagnostics.

```bash
# All of these are equivalent ways to raise the node budget for one run:
looplab run task.json --max-nodes 30
looplab run task.json -s max_nodes=30          # --set works for ANY field, not just the named flags
LOOPLAB_MAX_NODES=30 looplab run task.json
echo "LOOPLAB_MAX_NODES=30" >> .env && looplab run task.json
```

> Structured values (lists/dicts) are JSON in all three string forms, e.g.
> `--set agent_surface='["*.py","*.json"]'` or `LOOPLAB_AGENT_SURFACE='["*.py","*.json"]'`. In a YAML
> file they are just native YAML lists/maps.

### One file for the whole run

Instead of a JSON task plus a wall of env vars, a single YAML (or JSON) file can describe both *what*
to solve and *how* to run it. Run it with `looplab run looplab.yaml`:

```yaml
out: runs/demo            # where the run is written
task:                     # WHAT to solve (the task spec; same fields as a task JSON)
  kind: dataset
  goal: predict `target` from the features
  direction: max
  data_path: data.csv
settings:                 # HOW to run it (any non-credential Settings field on this page)
  backend: llm
  max_nodes: 20
  policy: asha
```

A file with neither a top-level `task:` nor `settings:` key is treated as a bare task (the legacy JSON
format), so existing task files keep working. A document with either key is unified; a settings-only
document therefore has no task and is rejected by `run`. The file is **input only** — the run dir still records canonical JSON
snapshots, so `resume`/`replay` are unchanged. Precedence within one run: `--set`/flags **>** the
file's `settings:` **>** env/`.env` **>** defaults.

---

## Web editors, schema and concurrent saves

The owner Web UI does not build forms by reflecting arbitrary Python fields in the browser. It fetches a
server-owned curated catalogue with **165 of the 196 direct `Settings` fields in 10 groups**. The default
**Essential** disclosure mode contains 18 high-frequency keys; search spans all 164 catalogued keys.
Uncatalogued fields remain valid through environment/config/CLI inputs and are preserved by sparse Web
writes. Which fields are catalogued is not a matter of taste: every `Settings` field is either a row or
listed in `settings_ui_schema.py::SETTINGS_UI_SCHEMA_UNCURATED_FIELDS` with the reason the form omits it,
and the server refuses to start when a field is in neither. Every switch that lets an LLM judge END an
experiment — and the confidence bar that decides it — is always a row.

The packaged catalogue format is v1 and the HTTP/editor contract is v2. The schema response includes
Pydantic-derived validation bounds, the **default each row's help text was reviewed against** (verified
against the live model at load, home-relative for `memory_dir`/`knowledge_dir`), and a semantic revision
exposed as a weak ETag. That ETag only revalidates
immutable editor metadata: it is **not** a mutation revision and must never be sent as a save CAS token.

Global settings use two independent opaque mutation revisions:

- `settings_revision` covers the sparse non-secret overrides in `ui_settings.json`;
- `secret_revision` covers the owner-only write-only credential store. The API reports only whether the
  credential exists and never echoes its value.

The current Settings page sends the revision observed by that tab as `expected_revision`. The server holds
the local and required interprocess locks across read/compare/merge/validate/atomic-write, and returns
structured `settings_revision_conflict` or `secret_revision_conflict` 409 responses when another writer
won. The browser retains the draft, refreshes authoritative state, reconciles fields that were accepted, and
requires a deliberate retry; it never blindly replays an unknown mutation.

The per-run Config editor has a separate contract. Its GET metadata includes a 64-character SHA-256
`config_revision` for the complete `config.snapshot.json`; the current editor sends that value as
`expected_revision` on `PUT /api/runs/{id}/config`. Its own equivalent local/interprocess locking contract—not
the global Settings lock—covers the
read/compare/merge/write transaction. A stale value returns structured
`run_config_revision_conflict` with the current revision and writes nothing. The seven run-start-pinned
selection-treatment fields and `profile` remain read-only under the rules described above.

All three mutation tokens are optional at the raw HTTP boundary only for legacy clients. Omission preserves
serialized last-writer-wins compatibility; it is not the current Web UI contract and is not recommended for
new clients. See [Web UI](ui.md) for the visible conflict/recovery behavior.

## Profile (one-word preset)

| Setting | Env | Default | Description |
|---|---|---|---|
| `profile` | `LOOPLAB_PROFILE` | `default` | `default`/`fast` = lean defaults; `thorough` = turn the built quality/trust machinery on |

`profile` is a **named override bundle** over the product defaults. `default` / `fast` preserve those
defaults, which already enable the ordinary agent loop and the explicitly experimental Part IV/V concept,
cross-run read/advisory and proposal-only curation features documented below. `profile: thorough` additionally
turns on the normally-disabled quality/trust bundle — multi-seed confirmation (`confirm_top_k=3`,
`confirm_seeds=3`), the reward-hack / leakage / critic monitors **plus**
`trust_gate=gate` (a flagged win can no longer be selected as best), ablation-driven refinement
(`ablate_every=3`), the adaptive operator bandit (`operator_bandit`), and the proposal cues
(`complexity_cue`, `budget_aware`). Failure/watchdog reflection and reflection priors are already on in the
product defaults; the preset keeps those values on but does not newly activate them.

A profile is **config-first**: it only fills fields you did *not* set yourself, so any explicit
knob — in the file, on the CLI (`--set`), or via `LOOPLAB_*` — always wins. It deliberately touches
only quality/trust knobs, never spend (`max_nodes`/`eval_parallel` stay yours).

```bash
looplab run examples/dataset_task.json --set profile=thorough      # everything trustworthy, on
looplab run examples/dataset_task.json -s profile=thorough -s confirm_top_k=5   # profile, but k=5
```

## Search budget & loop shape

| Setting | Env | Default | Description |
|---|---|---|---|
| `max_nodes` | `LOOPLAB_MAX_NODES` | `8` | Candidate (node) budget for the search. Every attempt spends a slot — failed and aborted ones too — with ONE exception: a Card *speculation* build (`speculation_depth` > 0) that the freshness gate discards **and that can PROVE it never ran** is refunded, because it consumed no evaluation. Such a run can therefore show more than `max_nodes` node ids in the tree while still having run exactly `max_nodes` experiments; the extra ids are the discarded predictions, and the refund is bounded at one whole `max_nodes` so a discard loop cannot extend the run indefinitely. The proof is a durable pair — the creator's promise (`eval_start_boundary` on `node_created`) plus the absence of the `node_eval_started` row it promised — so a build killed mid-sandbox is charged rather than refunded. **Fail closed: no promise, no refund**, which is why the exception does not apply to every discard (see [Speculation and the `budget` receipt](#speculation-and-the-budget-receipt)). |
| `max_parallel` | `LOOPLAB_MAX_PARALLEL` | `1` | Legacy raw-config alias for `eval_parallel`. Retained for old files, CLI arguments, environment variables, and snapshots; prefer the canonical name in new configuration and governance. It is a TRUE alias — a source naming only `max_parallel` is promoted to `eval_parallel`, so setting it still selects the eval width even though `eval_parallel` now ships set to `0` (AUTO). Its own default (`1`) is only what you get by explicitly unsetting the canonical field. |
| `parallel_build` | `LOOPLAB_PARALLEL_BUILD` | `1` | Legacy build width. NOT a full alias of `llm_parallel` from a file/CLI/env/snapshot: it sets the concurrent-build width but does **not** activate the shared LLM broker (broker opt-in stays tied to an explicitly-spelled positive canonical `llm_parallel`). A layer naming it masks a **lower-priority layer's** `llm_parallel` with the legacy sentinel, so CLI > file precedence holds within that tier. It does NOT mask a canonical value no layer of that tier set: precedence is per field, so an exported `LOOPLAB_LLM_PARALLEL` still applies when your file/CLI only spelled the legacy width. Only the live Strategist path treats it as a full alias. Prefer the canonical name in new configuration. **Because it is not promoted and the canonical field now ships SET (`0` = AUTO), a source naming only `parallel_build` no longer changes the STARTUP build width** — canonical wins whenever it is set, and AUTO is set. Spell `llm_parallel` for a startup width, or set `llm_parallel` to `None` to re-enable this legacy fallback; the live Strategist/operator control path still honours `parallel_build` directly. |
| `eval_parallel` | `LOOPLAB_EVAL_PARALLEL` | `0` (AUTO) | **Canonical** concurrent EVALUATIONS width inside one Run (GPU/experiment consumer), independent from LLM concurrency. The default `0` is launch-time AUTO: **one experiment per detected GPU** (at least one) — **but only for a task that can use one**: an adapter that declares itself CPU-locked (`gpu_capable() -> False`, see below) has no GPU-derived width, so its AUTO settles to serial `1` rather than reading a GPU count that cannot serve it. "Detected" means what CUDA will actually expose: an all-ordinal `CUDA_VISIBLE_DEVICES` fence is truncated left-to-right at the first index the box does not have, exactly as CUDA itself does, so a typo'd `CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7` on a two-GPU host derives `2` rather than an `8` that no child process could ever see (and that `run_started` would then pin permanently). UUID/MIG tokens, and any box whose GPU inventory cannot be probed, are left exactly as spelled. Set it (or its true alias `max_parallel`) for a fixed width — an explicit number is always honoured, including CPU-parallel evals on a CPU-locked task — or to `None` to fall back to the legacy `max_parallel` field default. `None` is **not** AUTO: it is the durable legacy mode, so a snapshot spelling `eval_parallel: null` resolves to `max_parallel` (default `1`) and, against a pinned log, refuses as an explicitly spelled `1`. It is the RESOLVED integer that `run_started` pins, so a resume on a differently sized box continues the run's own width instead of re-deriving one (engine invariant #6); an explicitly spelled width that disagrees with the pin refuses the resume, and `budget_extend` is the durable way to change it mid-run. A live Strategist/operator update of `0` settles to safe serial width `1` instead of re-reading mutable hardware. Separate local GPU-owning Runs in the same OS-user filesystem namespace serialize through a pool-wide lease; other users, containers, and hosts need external admission. The width alone does not decide whether a node reserves a device: a task adapter that declares itself CPU-locked (`gpu_capable() -> False` — the toy, regression, classification, timeseries and offline MLE-bench adapters do) reserves nothing for an undeclared footprint and never takes that lease, so an offline run on a GPU box is not serialized behind a neighbour's training job. |
| `llm_parallel` | `LOOPLAB_LLM_PARALLEL` | `0` (AUTO) | **Canonical** total concurrent LLM-provider-call budget, independent from eval concurrency; its settled value also controls node-build fan-out. The default `0` is launch-time AUTO: build fan-out follows the resolved `eval_parallel`, while the FOREGROUND research overlap stays unbounded — AUTO is **not** a positive value, so it does **not** activate a finite shared total. AUTO fan-out applies only where there is provider latency to overlap: when **no role calls an LLM at all** (`--backend toy` — the offline Toy/templated pair) it settles to serial `1`, which is what keeps the engine the sole writer of the event log and therefore keeps its byte ORDER reproducible (CLAUDE.md engine invariant #1) — the property the offline smoke relies on. A settled width of `1` governs the log's WRITER, not a whole run's content: `looplab bench` additionally varies with cross-run memory (the first suite into an empty `LOOPLAB_MEMORY_DIR` has no prior lessons and so lacks one `lessons_reconciled` event that every later suite has), and `benchmark.json` is never byte-identical because it records `eval_seconds`/`wall_seconds` — see `looplab/bench.py` for what IS reproducible there. "No role" means neither stage: under the shipped `unified_agent=true` one object plays both, and its `client` forwarder describes only the Developer stage, so an adapter with a *templated Developer but an LLM Researcher* (classification, regression, timeseries) used to be misread as offline and pinned to `1` while calling the provider once per node. The per-stage backends are inspected now. An explicitly spelled width is still honoured as spelled. Set it to `None` to fall back to legacy `parallel_build` for build fan-out, also without a shared total; unlike `max_parallel`, `parallel_build` is NOT promoted here, so it cannot reach this field on its own. It is the RESOLVED integer that `run_started` pins, on the same invariant-#6 terms as `eval_parallel`. A live Strategist/operator update of `0` settles both the total and build width to `1`. Only an explicitly-spelled POSITIVE canonical value activates the shared multi-lane broker — but the per-lane BACKGROUND caps (`deep_research`/`novelty_dedup`/`enrichment`, one concurrent call each, which is what bounds the two per-eval live-log watchdogs) apply with or without that total; the `build` and `engine` lanes stay unbounded until a positive value sets one. That asymmetry is about the PRODUCER, not the prompt: a cap where there is no budget to be fair about serializes foreground work, so anything the eval or build path blocks on belongs in `engine` — including the per-eval inter-stage check, which declared `enrichment` until the caps were unwelded from the total and then serialized every concurrent eval's stage gate. `core/llm_broker.py::BACKGROUND_LANE_PRODUCERS` registers what may declare a capped lane. |
| `train_monitor` | `LOOPLAB_TRAIN_MONITOR` | `true` | Per-eval background observer that tails the live training log while a (long) declared command stage runs. Its alert is fold-ignored and cannot directly change lifecycle, champion selection, or replay; when `watchdog_reflection` is on, the raw diagnostic can still advise a later Researcher prompt and thereby affect future proposals. No-ops without an LLM client or on the solution.py path |
| `train_monitor_interval_s` | `LOOPLAB_TRAIN_MONITOR_INTERVAL_S` | `600.0` | Base monitor tick cadence in seconds; the effective cadence adapts to the per-experiment budget and can only be tightened by this. No-op unless `train_monitor` is on |
| `train_monitor_kill` | `LOOPLAB_TRAIN_MONITOR_KILL` | `true` | INTERVENTION: let the monitor tree-kill a training it judges `broken` (diverged / silent CPU fallback / not learning) early. The node then fails normally (`reason=monitor_broken`) with no repair and no retry, so the gate is deliberately narrow. **Which log may kill:** only one the eval plan can PROVE is the run's own training — the classic single-command eval (`eval.log`, one command that trains *and* scores), or a one-stage pipeline, which is that same shape wearing a stage name. In a MULTI-stage pipeline the last stage is the scorer (that is where `command_eval` reads the metric) and every earlier stage is a work stage the engine cannot prove is the training step, so its verdicts stay **advisory** — a `data_prep` stage printing framework warnings and a flat loss draws a confident `broken` even when the prompt names the stage. `setup.log` (dep install), the scorer, and any filename with two possible writers (a stage named `setup`) are never judged at all; a log the monitor cannot attribute is advisory. **On top of that**, a kill must be CONFIRMED by a second consecutive `broken` verdict about the same log — a prompt re-look, not another full cadence — where a tick that produced no parseable verdict (endpoint failure, an answer outside the schema) re-arms the gate from zero, and the armed re-look is bounded to one identical re-ask and a five-minute window. Off = observe only |
| `train_monitor_kill_confidence` | `LOOPLAB_TRAIN_MONITOR_KILL_CONFIDENCE` | `0.8` | Minimum verdict confidence (0–1) required before a `broken` verdict triggers an early kill. One of four conjuncts — see `train_monitor_kill` for the stage-scope and confirmation requirements that apply on top |
| `asha_live` | `LOOPLAB_ASHA_LIVE` | `true` | ASHA live-curve watchdog for command evals using `stdout_json` or `stdout_regex`: read the latest intermediate objective metric and rank it against finished siblings at the same declared resource value. Other metric readers have no live observation path. Advisory (a fold-ignored `asha_rank` diagnostic + span) unless the stricter kill contract below is satisfied; with `watchdog_reflection` on, the raw diagnostic can also advise a later proposal without changing the current champion. Needs at least `asha_live_min_siblings` comparable finished nodes. Library default off |
| `asha_live_kill` | `LOOPLAB_ASHA_LIVE_KILL` | `true` | Intervention, only for `stdout_json` with an explicit `metric.resource_key`: a node whose comparable intermediate metric stays below the bar past the grace window becomes a candidate for an early stop (fails it `reason=asha_underperforming`). The rank test is only the evidence — the stop itself needs a confident `stop` verdict from the LLM judge (see `asha_live_kill_confidence`), so no LLM client means no kill. `stdout_regex` can be ranked but not killed; without a resource key the watchdog remains advisory |
| `asha_live_quantile` | `LOOPLAB_ASHA_LIVE_QUANTILE` | `0.5` | The rank bar sits at this quantile along a WORST→BEST ordering of finished siblings' finals: `0.5` = the median; SMALLER lowers the bar toward the WORST peer so it is more conservative (`0.0` = only stop a node worse than the worst finished peer); LARGER is more aggressive |
| `asha_live_min_siblings` | `LOOPLAB_ASHA_LIVE_MIN_SIBLINGS` | `3` | Minimum finished sibling nodes required before ASHA ranks at all (never acts on too little evidence) |
| `asha_live_kill_confidence` | `LOOPLAB_ASHA_LIVE_KILL_CONFIDENCE` | `0.8` | Minimum confidence (0–1) required from the ASHA judge's `stop` verdict before a flagged node is actually killed. Once the rank test fires past the grace window, the judge is shown the node's live curve, the same-resource sibling values and computed bar, the objective's direction, the other metrics the run is printing, and the training monitor's latest health verdict, and answers `continue`/`watch`/`stop` (a fold-ignored `asha_verdict` diagnostic + span). Because it is consulted only INSIDE the rank gate it can never stop a node the quantile test would have spared — it can only spare one the quantile test would have killed |
| `timeout` | `LOOPLAB_TIMEOUT` | `30.0` | Per-evaluation wall-clock limit (seconds) |
| `max_eval_timeout` | `LOOPLAB_MAX_EVAL_TIMEOUT` | `3600.0` | Hard ceiling for a Researcher-authored per-node `eval_timeout`, applied after the `agent_control.timeout` permission gate. The run-wide `timeout` remains the fallback when no permitted override is supplied. The one-hour default admits existing heavy-model requests while remaining below the sandbox's defensive 24-hour subprocess ceiling. |
| `sweep_timeout_mult` | `LOOPLAB_SWEEP_TIMEOUT_MULT` | `8.0` | A sweep node (a grid in one process) gets this × `timeout` |
| `eval_stall_timeout_s` | `LOOPLAB_EVAL_STALL_TIMEOUT_S` | `1800.0` | STALL watchdog cap (seconds): a stage that is completely SILENT on stdout/stderr for this long — while still alive and below its wall-clock deadline — is tree-killed early with a STALLED marker (a hung dataloader/deadlock dies in minutes instead of burning a multi-hour timeout). The per-stage window is `min(this, the stage's own timeout)`. Set to `0` to DISABLE the watchdog (only the hard deadline applies) — for a legitimately quiet non-Python stage (block-buffered stdout, a script logging only to its own file). Threaded into the eval and surfaced to the Developer so its code emits periodic progress to stay alive |
| `n_seeds` | `LOOPLAB_N_SEEDS` | `3` | Seeds per evaluation / rung-0 width |
| `max_seconds` | `LOOPLAB_MAX_SECONDS` | — | Hard wall-clock ceiling for the whole run |
| `max_eval_seconds` | `LOOPLAB_MAX_EVAL_SECONDS` | — | Hard ceiling on cumulative time *inside* evals (survives resume) |

### One experiment per GPU — who decides, and what happens when two runs want the same cards

The three facts above are spread across four table rows and are asked about often enough to be worth
stating together.

**Who decides the width.** Two independent knobs, both defaulting to `0` = AUTO, both settled ONCE at
run start and then pinned into `run_started` (engine invariant #6, so a resume on a differently sized
box continues the run's own width rather than re-deriving one):

| axis | what it bounds | AUTO resolves to |
|---|---|---|
| `eval_parallel` | concurrent EVALUATIONS — the GPU/experiment consumer | **one experiment per detected GPU** (at least 1) |
| `llm_parallel` | concurrent node BUILDS (and the shared provider budget) | the settled `eval_parallel`, so a build fan-out never outruns what can be evaluated |
| `speculation_depth` (`-1` = AUTO) | speculative prefetch backlog | the settled `eval_parallel` |

So on a two-GPU box, the shipped defaults already run **two experiments side by side, one per card**,
and `engine/evaluate.py::_evaluate` pins each concurrent eval to a DISTINCT GPU through
`CUDA_VISIBLE_DEVICES` — a framework left to see both cards typically pins itself to one and leaves
the other idle, which is why the width alone would not parallelize anything.

**One exception, deliberately.** AUTO means "let the BOX decide", so it only reads a GPU count where
the box is the constraint: a task adapter that declares itself CPU-locked (`gpu_capable() -> False`)
settles AUTO to serial `1`. Deriving a width from hardware that cannot serve the work is a category
error, and it cost determinism — two concurrent toy evals finish in wall-clock order, so the offline
smoke produced a different `node_evaluated` order run to run. An explicitly spelled number is always
honoured, CPU-parallel evals included.

**The width is NOT derived from the proposal.** It is settled from hardware at run start, once, for
the whole run. A per-Card *footprint* (`{"gpus": N}` on the idea/Card) does affect that node's device
reservation and admission, but nothing lets a Researcher proposal widen or narrow the run. Making the
width follow what the proposals actually ask for is an open item — see
[the operator backlog](../28-operator-backlog-2026-08-11.md).

**Two runs, one box: the second one WAITS.** GPU admission is serialized by a single pool-wide lease,
`/tmp/looplab-gpu-pool-<uid>.lock` — one file per OS user, exclusive across processes
(`engine/resources.py`). So if run A is resumed while run B is mid-experiment:

* run A **waits**, it does not fail, and it does not time out. `_wait_for_gpu_change` re-polls every
  0.5 s for as long as B holds the pool — potentially hours.
* The wait is announced at WARNING every 30 s, naming the lease path, the holding PID and how long it
  has waited. Before that notice existed this was completely silent and was repeatedly misdiagnosed
  as a deadlock: the run appends `setup_finished`, creates its nodes, and then nothing happens.
* **`eval_parallel=1` does not keep you out of the queue.** A serial run still claims the whole pool
  for a `gpu_capable` task. What keeps work out of it is declaring `{"gpus": 0}` on the idea/Card, or
  fencing the whole run with `CUDA_VISIBLE_DEVICES=`.
* The lease is per OS user and per filesystem namespace. Other users, containers and hosts need an
  external scheduler — LoopLab does not coordinate across them.

A plain repair/resume that needs no GPU is unaffected: a CPU-locked adapter never takes the lease.

### What blocked speculation from being the default (fixed 2026-08-05)

`card_driven_selection` has shipped `true` since 2026-08-04, but speculation needs **both** that and a
positive `speculation_depth` — so until 2026-08-05 the Card lane pre-built nothing on stock defaults and
the shipped `card_driven_selection` was effectively inert for *selection*. `speculation_depth` now
defaults to `-1` (AUTO) and everything around it was already in place: AUTO resolution, the `run_started`
pin, the node-budget refund, and three AUTO settle-to-off rules.

> **Superseded on 2026-08-07, and this is the sentence to correct:** "inert for selection" was true
> because the only writer of selectable Card *inventory* sat behind the prefetch gate, not because
> selection genuinely needs a prefetch. It no longer does — `card_driven_selection` mints and selects
> from the queue on its own, and `speculation_depth` only decides who builds the selected Card. That
> matters beyond history, because AUTO settles itself to `0` in three documented cases (a build whose
> roles call no LLM, a policy other than `greedy`, a run directory with no run id) and can ratchet to
> `0` mid-run: in each of those the run used to change *selector* silently while `run_started` still
> pinned `card_driven_selection: true`. Measured over the shipped corpus: seven such runs produced
> **0** selection-ready Cards across 27 nodes, against 24 in the four speculation runs.

What blocked it was a defect in the Card **debug** anchor that only a default would make everyone's
problem. `events/card_ledger.py::_card_debuggable_leaf_ids` disqualified a failed node the moment it had
*any* child — and a receipt-bound `debug` Card's own work item is such a child. So the instant the
Card's node existed, the Card's own anchor died and it folded to `action_receipt_incomplete`. The
ordinary lane never noticed, because it never re-checks a Card after its node exists. The speculative
freshness gate *does* — that is its entire job — so **every speculative `debug` prefetch was superseded
on sight**, and the lane then authored a fresh, permanently unselectable Card every loop turn until the
runaway guard ended the run with `stuck: node creation not converging`.

Measured on a real 2-GPU repo run launched at AUTO: **2 nodes of a 12-node budget**, three Cards, then
"stuck". Reproduced offline at depth `1` on a task whose first node crashes: 2 nodes and 88 dead
Cards, where the identical task with speculation off completes 12 of 12. Any run whose node *failed*
reached this, which on a real repo task is routine.

**Fixed on 2026-08-05**, in two halves that have to agree with each other. A Card's own work item no
longer disqualifies its own parent — every *other* child still does, so a failed node with a real
sibling child is closed exactly as before. And a child the Card lane's policy **cannot see** is not
counted as a child at all: the fold now calls the same
`node_counts_toward_card_budget` predicate that builds the policy's node universe (it lives in
`core/models.py` so replay can reach it — `events` may not import `search`), instead of the two views
disagreeing about whether the failed parent still had work available. The original offline
reproduction — the one whose failed node's only child was the Card's own discarded prefetch — runs
**16 nodes minted, 12 charged, 11 evaluated, normal finish** at depth `1` after that, four prefetches
discarded and refunded, so its budget denominator matches the speculation-off control exactly.

The first cut of that fix shared only *one clause* of the predicate — the discarded-prefetch proof
`is_unevaluated_speculative_discard` — and left the fold re-deriving the other three by omission, so
the identical runaway reopened the same day on a **tombstoned** child, a **constraint-gated**
(`feasible=False`) child and a **trust-gated** (`breed_excluded`) child. Sharing the predicate itself
is what closes all four at once, and what stops a fifth class from reopening it: the L3 budget, the
policy's universe and the fold's leaf test now move together by construction. The offline
reproduction at depth `1` goes from **7 nodes frozen with 84 dead `debug` Cards on one parent** (the
run dies on `stuck: 1 action(s) planned for 84 consecutive loop turns without creating a node`) to a
**normal 12-of-12 finish**, on both the tombstoned and the constraint-gated shape.

The divergence itself does not need speculation — it is a disagreement between the policy view and
the fold, and it reproduces with `card_driven_selection` alone. What speculation adds is the
escalation: with a prefetch backlog, a raw debug proposal is *staged as a Card* instead of being
built, so an unselectable Card is re-authored every turn. With speculation off the same proposal
falls through to a serial build, so the run still finishes — but the Card the lane already owns for
that repair is never claimed. Measured at depth `0` and at AUTO on both shapes: the staged Card
stays on `action_receipt_incomplete`, the node the lane builds for the same repair carries no
`card_id`, and the board ends with one more orphaned Card than it should. After the fix that node
claims the Card (its evidence, its `work_terminal` receipt) and the extra Card is not authored.
(Those depth-`0` numbers were taken before 2026-08-07, when a depth-`0` run staged no Cards at all
and every node was built from a raw policy action. A depth-`0` run now mints, selects and serially
claims the same Card a depth-`1` run prefetches, so "the same proposal falls through to a serial
build" is still the outcome — it just goes through the queue on the way.)

The **runaway guard was split in the same change**, because it is what misdiagnosed the incident above:
it now charges only nodes the log says were **minted** (`node_created` rows), so a Card lane that stages
and elects without minting anything is no longer reported as `stuck: node creation not converging` when
not one node had been created. Its companion bound covers the other half — a create lane that keeps
*planning* work and minting nothing ends the run with `stuck: N action(s) planned for M consecutive loop
turns without creating a node` instead of looping forever. Both use the same generous cap
(`max(max_nodes, 4) × 3 + 50`), so neither false-trips on operator injects or wide seed batches.

With the anchor proven, the default was flipped in the same batch: `speculation_depth` ships `-1`
(AUTO) as of 2026-08-05, so a stock `greedy` LLM run pre-builds one Card per evaluation lane and
`card_driven_selection` is no longer inert for selection. Runs whose nodes fail no longer degrade —
the `budget` receipt below is how you check what speculation actually did for you.

### Adaptive AUTO depth

**A prefetch only pays when there is provider latency to hide behind a running evaluation.** AUTO's
startup rule keys off the settled `eval_parallel` — *how many experiments can run at once* — which is a
capacity question, not that one. Measured on `examples/classification_task.json` **as it shipped
before 2026-08-05** (the flat two-blob variant — that example is now the concentric-rings task, see
[Task reference](tasks.md#classification)), same command, both arms 8/8 nodes and the identical
champion (node 7, metric 0.925):

| | LLM calls | tokens | wall clock |
|---|---|---|---|
| AUTO → depth 1 | **109** | **1,265,911** | 2348.8 s |
| `-s speculation_depth=0` | 75 | 817,201 | 2448.6 s |

45% more calls and 55% more tokens for a 4% wall-clock saving. Evaluations on that task took ~0.1 s,
so there was never anything to overlap — and the overhead is *not* waste from wrong predictions (one
stale prefetch in nine requests); it is fixed Card-lane cost. (The reworked rings example evaluates
in 0.05–0.6 s, which changes none of this: the argument is "an evaluation far shorter than provider
latency hides nothing", and sub-second still qualifies.)

So AUTO now settles itself down a third time, on the same argument it already applies twice ("a build
whose roles call no LLM has no provider latency to overlap"). Once the run has **measured itself** —
at least two evaluated nodes and two build spans, both read off its own event log — it compares the
*median* evaluation against the *median* build (`node_building` → `node_created`). When one evaluation
cannot hide even **10 %** of a build, the depth ratchets to `0` and the run continues serially.

A **ratio**, not a number of seconds, on purpose: "fast" only means anything relative to the latency
the overlap is supposed to hide, so a run against a slow endpoint keeps prefetching at eval durations
a fast endpoint would not justify. And a **one-way ratchet**: it can only ever go down. A symmetric
rule would oscillate with every slow-then-fast node, and each oscillation is a durable change to the
run's *search treatment*, not a tuning knob — while the harm is asymmetric (prefetching on a fast task
costs tokens for nothing; not prefetching on a slow one costs some wall clock and nothing else).

**Each move is an event.** `speculation_depth_settled` carries the resolved integer *and* the evidence
it was derived from, so `replay.fold` reads the outcome and re-measures nothing: a resume on a
different host continues under the treatment this run chose, exactly as the startup pin guaranteed
before. In practice a run emits at most one of these rows, because the rule's whole finding is "there
is nothing here to overlap" and the depth it resolves to is `0`; the fold is nevertheless written for
many, which is what makes a duplicated or replayed row inert. **AUTO only** — a spelled
`speculation_depth` is honoured (or refused) exactly as spelled, and never settles.

**The launch pin and the settle are two different facts, and the fold keeps them apart.** A run
records both: what `run_started` committed, and the floor its own ratchet narrowed itself to. The
effective depth is the first capped by the second, derived from two independent folded values rather
than by overwriting one — which is what makes the pair genuinely order-tolerant and idempotent, and
what lets a resume tell "the operator changed the treatment" (refused) from "the run narrowed itself"
(adopted). A log with no settle rows keeps precisely the depth `run_started` pinned; a settle row
naming a depth *above* the pin is inert.

**The ratchet is one-way for the life of the run, and no resume flag lifts it.** Replay applies the
recorded settle every time, so re-running with `-s speculation_depth=N` on the same run directory
changes nothing — spelling the depth `run_started` pinned is accepted and the run still executes at
the settled depth; spelling any *other* depth is refused, because the run-start record owns the
treatment (invariant #6). To keep the prefetch on, spell the depth at **launch**: a spelled depth
never settles. The warning the ratchet prints says exactly this.

To watch for it: `grep speculation_depth_settled RUN_DIR/events.jsonl`, or read `speculation.depth` in
the run's `budget` receipt, which reports the depth that was *in force at the end*. The run's HTTP
config (`GET /api/runs/{id}/config`) reports the **launch** setting instead — including the `-1` AUTO
sentinel, which is a standing request the pin is one resolution of, and which the config editor
therefore never rewrites.

### Speculation and the `budget` receipt

Positive `speculation_depth` pre-builds the Card the policy predicts you will pick next. A prediction that
misses is thrown away before it reaches a sandbox and its `max_nodes` slot is refunded (see `max_nodes`),
so speculation is not supposed to cost you experiments. **Every run measures whether that actually held**
and writes the answer into its own `budget` finalization receipt — one fold-ignored `budget` event per
run, in `events.jsonl`, under the key `speculation`. No GPU calibration ceremony required:

| Key (inside `budget.speculation`) | Meaning |
|---|---|
| `speculation.depth` | The run's resolved `speculation_depth` (`0` when speculation was off — every other key is then `0` too) |
| `speculation.requested` | Prefetch requests the consumer issued |
| `speculation.committed` / `.stale` / `.producer_failed` | Producer outcomes: a build landed / was already stale at commit / the producer failed |
| `speculation.evaluated` | Speculative builds that were admitted and really ran — correct predictions, ordinary experiments |
| `speculation.discarded` | Speculative builds the **build lifecycle** threw away without an experiment coming out — superseded by the freshness gate, frozen, batch-cancelled, a build crash, a lost commit, a proposal that could not form an action (a miss). **Not** a speculative node that ran and failed: that is an experiment result |
| `speculation.abandoned` | Committed prefetches still **pending** when the run finished — the consumer stopped admitting fresh work (operator `stop`, wall deadline, `max_eval_seconds` crossed) and these were never terminalized |
| `speculation.refunded` | Discards that proved they never ran and got their `max_nodes` slot back |
| `speculation.charged_discards` | **The regression signal.** Speculative builds that produced no experiment *and* still spent a node-budget slot |

`charged_discards` counts both `discarded`-but-not-refunded builds **and** every `abandoned` one, so it is
routinely non-zero on a run you stopped early or that hit its wall/eval-seconds deadline with prefetches in
flight — an abandoned prefetch bought a Developer call and no experiment, which is real spend even though
nothing malfunctioned. Treat a *growing* `charged_discards` on runs that finish naturally as the signal
worth chasing; the clean-refund case is `charged_discards == 0`.

!!! note "A crashing experiment is not a discard (fixed 2026-08-05)"

    `discarded` used to mean *any* failed speculative node, which — with speculation shipping on by
    default — made `charged_discards` positive for every ordinary crash. A live run reported
    `discarded: 1, charged_discards: 1` where the "discard" was a real experiment that ran five
    evaluations and died on a CUDA device-side assert. A signal every crash trips measures crashes, not
    speculation. A node now counts as discarded only when its terminal was written by the **build
    lifecycle** (`search/speculation_quality.py::SPECULATION_DISCARD_REASONS` — a registry, scanned
    two-way against the engine terminals that write it) **or** when the log shows it never ran at all,
    whatever its reason says. Over-reporting is still the safe direction for an unreadable state; what
    changed is that it now over-reports on *speculation* evidence.

!!! warning "A run started before 2026-08-04 and resumed today will show different budget numbers"

    The refund now requires a durable proof that the build never ran: the creator's promise
    (`eval_start_boundary`, stamped on `node_created`) plus the absence of the `node_eval_started` row it
    promised. Before that, a build killed mid-sandbox was byte-indistinguishable from one that never
    started, and 40 GPU-minutes of real work could be refunded after a crash. Logs written by the older
    build carry **no promise**, so their speculative discards are charged and never refunded — deliberately
    fail-closed. Nothing about the run changed; its *accounting* did. Expect `refunded` to drop and
    `charged_discards` to rise on such a resume, and compare budget numbers only within one build.


## Backend & roles

| Setting | Env | Default | Description |
|---|---|---|---|
| `backend` | `LOOPLAB_BACKEND` | `llm` | `llm` (live model — the default; a real run needs a reachable endpoint, see **LLM endpoint** below) or `toy` (offline optimizer, no model calls at all). A CLOSED set: any other value — including a mis-cased `LLM` — is rejected at config time rather than falling through to the offline toy roles. |
| `developer_backend` | `LOOPLAB_DEVELOPER_BACKEND` | `default` | `default`, or an external agent: `opencode` / `aider` / `goose` / `continue` |
| `unified_agent` | `LOOPLAB_UNIFIED_AGENT` | `true` | One engine-facing control facade/object implements Researcher + Developer (+ Strategist/pilot) over stage-specific clients, tools and local contexts. It is not one shared cross-stage conversation identity |
| `agent_drives_actions` | `LOOPLAB_AGENT_DRIVES_ACTIONS` | `true` | The agent picks the next macro action within a pure legal-action gate |
| `card_driven_selection` | `LOOPLAB_CARD_DRIVEN_SELECTION` | `true` | The Card queue owns macro-action selection. Set `false` for the legacy policy/unified-pilot action path. The value is pinned by `run_started` (changing the selector on resume would mix two search treatments in one run); when both action flags are enabled, Card selection takes precedence over `agent_drives_actions`. **This flag alone maintains the queue** — it mints each proposal as durable, selectable Card *inventory* first and only then selects and builds it, independent of `speculation_depth`. That was not true before 2026-08-07: the only writer of selectable inventory sat behind the prefetch lane, so a run with the flag on and the depth settled to `0` silently fell back to `policy.next_actions` while `run_started` still recorded `card_driven_selection: true` (measured over the shipped corpus: **0** selection-ready Cards across 27 nodes in seven such runs). |
| `speculation_depth` | `LOOPLAB_SPECULATION_DEPTH` | `-1` (AUTO) | Live-prefetch-backlog cap: outstanding requests plus committed pending speculative Card builds not already admitted to the current consumer session. `-1` = **AUTO**, resolved at startup to the settled `eval_parallel` (one prefetch per concurrent evaluation lane, clamped `1`–`64`; a task declaring `gpu_capable() -> False` settles that width to `1`) — it needs its own sentinel because `0` already means fully off; `-1` is **the default** since 2026-08-05; `0` = off; `1`–`64` = that exact cap. It is the RESOLVED integer that `run_started` pins, so resume cannot mix execution treatments after a config edit and a differently sized box continues the run's own treatment. **AUTO also re-resolves DOWNWARD mid-run** once the run has measured itself — see [Adaptive AUTO depth](#adaptive-auto-depth) — and every such move is a durable `speculation_depth_settled` event, so replay and resume reproduce the treatment the run actually used rather than one re-derived from the resuming box. That settle is kept SEPARATE from the run-start pin in the folded state, so a resume can tell a run that narrowed itself (adopted, silently) from an operator changing the treatment (refused); it is a one-way ratchet no resume flag lifts, and this field is refused by the HTTP config editor after launch — except for the `-1` sentinel itself, which is the standing request the pin resolves and is therefore never rewritten. Takes effect with `card_driven_selection=true` (also the default) and `policy=greedy` (the speculative freshness test asks the policy for the counterfactual next action). It adds PREFETCH to the Card lane and nothing else: since 2026-08-07 `card_driven_selection` maintains and selects from the queue on its own, so `0` here means "select from the queue, build serially", not "no queue". **AUTO settles itself to OFF in three cases**, all of them "this run cannot usefully prefetch": a build whose roles call **no LLM** (`--backend toy` and templated roles — a prefetch exists to overlap the Developer's *provider latency* with the running eval, and a local build has none, so the backlog buys nothing and only costs the offline smoke its byte-reproducible event order); a **policy other than `greedy`**; and a run directory with **no run id**. The last two are hard refusals for a *spelled* depth, so AUTO settling instead is what keeps `--policy mcts` (or `evolutionary`/`asha`/`bohb`) startable under the default. An explicitly spelled value is always honoured — or refused — exactly as before, which is how the `looplab speculation-gate` calibration pairs still run at depth `1` on the Toy adapter. The backlog cap bounds outstanding *work*, not producers: exactly one speculative build runs at a time, so the default adds at most one concurrent provider call to the eval window (measured through the real broker at `eval_parallel=2`: peak concurrent `build`-lane calls went 2 with speculation off to 1 with depth 2, because the Card lane replaces the parallel seed batch with a serial elect-then-prefetch spine). **Works on any task** — no receipt required: a prediction that misses is discarded *before* it runs, so it costs one Developer call, no evaluation and no GPU time, and its `max_nodes` slot is refunded (see `max_nodes`). The refund is *proven*, not assumed — a discard that cannot prove it never ran, and any prefetch still pending when the run finishes, is charged. [Speculation and the `budget` receipt](#speculation-and-the-budget-receipt) explains what each run reports and why an old log resumed today accounts differently. |
| `speculation_gate_receipt` | `LOOPLAB_SPECULATION_GATE_RECEIPT` | — | OPTIONAL. Absolute path to a local receipt produced by `looplab speculation-gate` from exactly three fresh depth-0/positive-depth calibration pairs (fixed seeds `0/1/2`) on the effective real GPU. It is the quality BENCHMARK's result — scorer fidelity, hit rate, divergence and normalized regret on the shipped quadratic Toy adapter — not a licence: positive `speculation_depth` runs without one. Supplying one is still a claim that must hold, so the engine always revalidates it (thresholds, self/implementation/environment digests, seven-field GPU identity, Greedy scope, and a full recomputation from its own raw paired runs) — but **what a failure means depends on the lane**. ON THE SHIPPED QUADRATIC TOY ADAPTER (the calibration lane) the receipt IS the authority: the run additionally binds to the exact measured runtime envelope (Settings profile, roles, sandbox, tested depth, `max_nodes`, runtime-scope digest), and a stale or forged receipt **refuses the run**. ON EVERY OTHER WORKLOAD your `speculation_depth` is the authority and the receipt is *inert in both directions* — it never authorizes and it never pins. A failing receipt is DECLINED (set to `None`) and the run proceeds in the product lane; a passing one changes nothing, including on resume. A hard raise there would take down a whole Repo/GPU run over an attestation the run does not need, and pinning the receipt's whole-source implementation digest is what used to make such runs permanently unresumable after any source edit. The receipt path is intentionally not exposed as a casual Settings-UI field. |

Set `unified_agent` and `agent_drives_actions` both to `false` for the legacy split-role behavior.
These are no-ops unless `backend=llm`.

## LLM endpoint

| Setting | Env | Default | Description |
|---|---|---|---|
| `llm_model` | `LOOPLAB_LLM_MODEL` | `qwen3:8b` | Model id |
| `llm_base_url` | `LOOPLAB_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint (Ollama default). Changing it **alone** is refused when a key is configured — the credential must move with it, see [moving a run to a different endpoint](llm-and-agents.md#moving-a-run-to-a-different-endpoint) |
| `llm_api_key` | `LOOPLAB_LLM_API_KEY` | — | Secret; never serialized as a value. Local servers ignore it. Atomic with the row below: the pair is reselected from ONE source (process env, else `.env`), so setting this alone does not inherit the binding from the other source |
| `llm_api_key_base_url` | `LOOPLAB_LLM_API_KEY_BASE_URL` | — | Endpoint the secret above is bound to. A key travels only while the request still goes to this host, so a role/stage endpoint override drops the credential instead of sending it elsewhere. Dropped from config snapshots entirely (not masked) |
| `llm_temperature` | `LOOPLAB_LLM_TEMPERATURE` | `0.6` | Sampling temperature |
| `llm_parser` | `LOOPLAB_LLM_PARSER` | `tool_call` | Structured-output strategy (`tool_call`, with text fallback) |
| `llm_guided_json` | `LOOPLAB_LLM_GUIDED_JSON` | `false` | Use the endpoint's constrained decoding (vLLM/SGLang `guided_json`) |
| `llm_reasoning` | `LOOPLAB_LLM_REASONING` | `high` | Thinking depth: `""` (server default) / `off` / `on` / `low` / `medium` / `high` |
| `llm_reasoning_style` | `LOOPLAB_LLM_REASONING_STYLE` | `auto` | How to shape the request: `auto` / `qwen` / `effort` / `none` |
| `llm_reasoning_extra` | `LOOPLAB_LLM_REASONING_EXTRA` | `{}` | Raw fields merged into the request body (escape hatch) |
| `llm_stream` | `LOOPLAB_LLM_STREAM` | `True` | Stream the response (SSE) and reassemble it — bounds a stalled generation via an idle-guard watchdog; off = one blocking request |
| `llm_timeout` | `LOOPLAB_LLM_TIMEOUT` | `180.0` | Per-request inter-token idle limit (s): a stream with no new token for this long is aborted + retried |
| `llm_header_timeout` | `LOOPLAB_LLM_HEADER_TIMEOUT` | `45.0` | First-byte / response-headers window (s) for **streaming attempts**: bounds both the wait for response headers (a wall-clock guard on `create()`) and the first stream event, before the request is treated as stalled and failed over; clamped to `llm_timeout`. Non-stream attempts use the whole-call deadline (`llm_timeout` + this) instead |
| `llm_trust_env` | `LOOPLAB_LLM_TRUST_ENV` | `False` | Honor HTTP(S)_PROXY / NO_PROXY env for the LLM client. Default false = a direct connection (the internal endpoint needs no proxy) |
| `llm_cache` | `LOOPLAB_LLM_CACHE` | `False` | Serve identical **deterministic** (temperature-0) LLM requests from an in-process content-addressed cache (cuts cost on retry/panel/verify); sampling calls (temp>0) are never cached. Off by default |
| `compressor_model` | `LOOPLAB_COMPRESSOR_MODEL` | — | Model id for the history auto-summary compressor (blank = the shared chat model) |
| `compressor_base_url` | `LOOPLAB_COMPRESSOR_BASE_URL` | — | Endpoint for the compressor model (blank = reuse llm_base_url) |
| `context_budget_chars` | `LOOPLAB_CONTEXT_BUDGET_CHARS` | `1000000` | Compact the agentic tool-call history once it exceeds this many chars (~250k tokens, sized to the model's context window so reads stay in context); 0 = off |
| `agent_max_turns` | `LOOPLAB_AGENT_MAX_TURNS` | `0` | Max tool-loop turns before the emit is forced; 0 = unlimited (the agent loops until done) |
| `agent_emit_after` | `LOOPLAB_AGENT_EMIT_AFTER` | `300` | Convergence nudge: after N **investigation** turns (turns that actually ran a tool — a bounced emit or a lone `update_plan` does not count), prompt the agent once to stop investigating and emit (0 = off) |
| `agent_emit_force` | `LOOPLAB_AGENT_EMIT_FORCE` | `500` | Hard backstop: force the emit at N tool-calling turns — counting **every** turn that called a tool, so a model that only bounces on emit validation or updates its plan still terminates (0 = off) |
| `agent_time_budget_s` | `LOOPLAB_AGENT_TIME_BUDGET_S` | `0` | Wall-clock ceiling across an agent's tool-loop turns; 0 = no cap |
| `agent_stuck_detection` | `LOOPLAB_AGENT_STUCK_DETECTION` | `true` | **B1** — stop an agent that repeats the same call / ping-pongs / re-hits the same error with no progress (forces its emit). The safety net that makes unlimited turns safe |
| `agent_stuck_repeat` | `LOOPLAB_AGENT_STUCK_REPEAT` | `4` | Identical call+result turns in a row that count as "stuck" (min 2) |
| `agent_stuck_alternate` | `LOOPLAB_AGENT_STUCK_ALTERNATE` | `4` | Ping-pong cycles between two calls that count as "stuck" (min 2) |
| `agent_self_plan` | `LOOPLAB_AGENT_SELF_PLAN` | `true` | **C1** — expose a typed TodoWrite-style `update_plan` tool so a long-running agent keeps its own TODO, re-surfaced periodically. This now includes Deep Research: its default prompt asks the model to plan 2–4 working sub-questions and update them as evidence gaps close. |
| `agent_plan_reinject_every` | `LOOPLAB_AGENT_PLAN_REINJECT_EVERY` | `5` | How often (tool-loop turns) to re-surface the agent's current plan |
| `agent_auto_summary` | `LOOPLAB_AGENT_AUTO_SUMMARY` | `true` | **C2** — LLM-summarize the stale middle of the tool-loop history once it exceeds `context_budget_chars` (default ~1M chars ≈ 250k tokens). The ~120k-char high-water fallback applies only when `context_budget_chars` is **unset** (`None`) — with the shipped default of 1,000,000 it never fires; `context_budget_chars=0` means compaction **off**, matching its own row above |
| `developer_plan_decompose` | `LOOPLAB_DEVELOPER_PLAN_DECOMPOSE` | `true` | **C4** — the repo Developer first proposes an ordered plan of ATOMIC steps; a multi-step plan is executed step-by-step, each a FRESH bounded session building on the files so far (syntax-validated per write). Stops a big feature from making a non-converging model run away (writing+exploring without ever emitting `done`). A 1-step plan == the old single pass |
| `developer_plan_min_steps` | `LOOPLAB_DEVELOPER_PLAN_MIN_STEPS` | `2` | A proposed plan with ≥ this many steps runs step-by-step; fewer falls back to one session |
| `developer_plan_max_steps` | `LOOPLAB_DEVELOPER_PLAN_MAX_STEPS` | `8` | Cap on plan length (a runaway planner can't spawn 100 steps) |
| `developer_session_max_turns` | `LOOPLAB_DEVELOPER_SESSION_MAX_TURNS` | `500` | Hard per-session tool-turn ceiling for the repo Developer (the one write-heavy agent that can run away even with stuck-detection on — varied writes/reads never trip the repeat signal). Bounds the plan phase, each step, and the single-session fallback |
| `developer_session_time_budget_s` | `LOOPLAB_DEVELOPER_SESSION_TIME_BUDGET_S` | `1200` | Wall-clock ceiling per developer session (20 min); a model that never emits `done` fails cleanly with the code it wrote |
| `phase_handoff_summary` | `LOOPLAB_PHASE_HANDOFF_SUMMARY` | `true` | Per-node phase coordination: each exploration phase (Researcher·propose → Developer·stages → plan) ends with ONE call that distills its transcript into a brief injected into later phases — even across the role boundary — so they trust it instead of re-reading. Terminal phases (implement / repair) consume but don't summarize (no wasted call); the propose brief is produced only when the in-house repo Developer follows. There is no read cache — every read executes fresh (oversized results carry an explicit truncation marker) |

### Per-role / per-stage models

Run the Researcher and Developer on different models or endpoints (e.g. a coder model for the
Developer, a fast model for breadth). Blank values fall back to the shared `llm_*`.

| Setting | Env | Default | Description |
|---|---|---|---|
| `researcher_model` / `developer_model` | `LOOPLAB_RESEARCHER_MODEL` / `LOOPLAB_DEVELOPER_MODEL` | — / — | Per-role model id (blank = shared `llm_model`) |
| `researcher_base_url` / `developer_base_url` | `LOOPLAB_RESEARCHER_BASE_URL` / `LOOPLAB_DEVELOPER_BASE_URL` | — / — | Per-role endpoint (blank = shared `llm_base_url`) |
| `strategist_model` / `strategist_base_url` | `LOOPLAB_STRATEGIST_MODEL` / `LOOPLAB_STRATEGIST_BASE_URL` | — / — | The Strategist's own model and endpoint (blank = shared). Before these existed it had a per-role *temperature* but no model of its own, so it always ran on `llm_model` however the other roles were pointed |
| `researcher_temperature` / `developer_temperature` / `strategist_temperature` | `LOOPLAB_RESEARCHER_TEMPERATURE` / `LOOPLAB_DEVELOPER_TEMPERATURE` / `LOOPLAB_STRATEGIST_TEMPERATURE` | — | Per-role sampling temperature (blank = shared `llm_temperature`). Raise the Researcher for idea breadth, lower the Developer for code determinism. Deep-Research follows the Researcher's value |
| `agent_stage_models` | `LOOPLAB_AGENT_STAGE_MODELS` | `{}` | Unified-agent per-stage model map. Exact `AGENT_STAGE_KEYS`: `propose`/`implement`/`repair`/`strategy`/`pilot`; unknown or mis-cased keys and non-string/blank values are rejected. Only takes effect when `unified_agent` is on. `implement` and `repair` are independent: pointing them at different models gives the repair stage its own Developer |
| `agent_stage_base_urls` | `LOOPLAB_AGENT_STAGE_BASE_URLS` | `{}` | Unified-agent per-stage endpoint map with the same exact five-key registry; unknown or mis-cased keys and non-string/blank values are rejected. Precedence: stage map > per-role field > shared default, per property — so a stage that overrides only the endpoint keeps its role's model and temperature |

Fresh `Settings`, environment JSON, config files, and launch API settings all enforce that exact
five-key registry. Historical legacy and current `config.snapshot.json` files remain resumable if
they contain a stage key this build no longer knows: LoopLab emits an explicit warning and filters
the key from the effective resume settings only; it does not rewrite the snapshot evidence.

See [LLM & coding agents](llm-and-agents.md) for full guidance.

### Connection profiles (only needed with more than one provider)

Skip this section if you run one model: keep setting `llm_model`, `llm_base_url` and the single
`LOOPLAB_LLM_API_KEY`, and nothing below changes anything.

A **profile** is a named connection — `{model, base_url, temperature, api_key_env}` —
that roles point at by name. It exists because per-role *models* alone cannot express per-role
*credentials*: two roles on the same provider may need different keys, budgets or subscriptions.

A profile stores `api_key_env`, the **name** of an environment variable, never a key value. That is
what makes the whole map safe to write into `config.snapshot.json`, serve over HTTP and render into
`LOOPLAB_*` — there is nothing in it to mask. A literal key inside a profile is rejected at startup,
and the variable name must look like a secret (`UPPER_SNAKE` containing `KEY`/`SECRET`/`TOKEN`/
`PASSWORD`/`PASSWD`/`CREDENTIAL`) so the sandbox's secret-name filter strips it from the environment
handed to generated candidate code.

```bash
export LOOPLAB_LLM_PROFILES='{
  "local": {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
  "coder": {"base_url": "https://api.provider.tld/v1", "model": "big-coder",
            "api_key_env": "LOOPLAB_LLM_API_KEY_CODER", "temperature": 0.1}}'
export LOOPLAB_ROLE_PROFILES='{"implement": "coder", "repair": "coder", "propose": "local"}'
export LOOPLAB_LLM_API_KEY_CODER=sk-...
```

| Setting | Env | Default | Description |
|---|---|---|---|
| `llm_profiles` | `LOOPLAB_LLM_PROFILES` | `{}` | Named connections: `name -> {model, base_url, temperature, api_key_env}`. `api_key_env` is a variable NAME, never a key. Empty = no profiles, everything as before |
| `role_profiles` | `LOOPLAB_ROLE_PROFILES` | `{}` | `role -> profile name`. Valid roles are `propose`, `implement`, `repair`, `strategy`, `pilot`, `researcher`, `developer`, `strategist`, `compressor`, `embed`; an unknown role or a missing profile fails loudly at startup |
| `llm_profile` | `LOOPLAB_LLM_PROFILE` | — | The profile every unbound role uses — a one-line move of the whole run to another provider |

**Precedence** — each property resolves on its own, first non-empty wins:

| Property | Order |
|---|---|
| model | `agent_stage_models[stage]` → `<role>_model` → profile `model` → `llm_model` |
| endpoint | `agent_stage_base_urls[stage]` → `<role>_base_url` → profile `base_url` → `llm_base_url` |
| temperature | `<role>_temperature` → profile `temperature` → `llm_temperature` |
| credential | profile `api_key_env` (read from the environment) → `llm_api_key`. **Not** independent: the profile's key travels only while the endpoint is still the one that profile would have used, so a `<role>_base_url` or stage-map override drops it rather than sending one provider's secret to another host |

The stage maps apply only when `unified_agent` is on. A role bound to a profile whose `api_key_env`
is unset fails **before the run's first paid call**, naming the variable and the role but never its
value; unbound profiles are not checked, since one machine-wide map may describe providers this run
never touches.


## Search policy & allocation

| Setting | Env | Default | Description |
|---|---|---|---|
| `policy` | `LOOPLAB_POLICY` | `greedy` | `greedy` / `evolutionary` / `mcts` / `asha` / `bohb` (`bohb` is the ASHA schedule wired to the surrogate proposer — setting it turns `surrogate_proposer` on for you) |
| `asha_eta` | `LOOPLAB_ASHA_ETA` | `3` | ASHA/BOHB reduction factor (keep top 1/η per rung) |
| `asha_rung_nodes` | `LOOPLAB_ASHA_RUNG_NODES` | `0` | Rung-0 width (0 = use `n_seeds`) |
| `surrogate_proposer` | `LOOPLAB_SURROGATE_PROPOSER` | `false` | BO-lite: propose by a k-NN surrogate over (params→metric). Works on any task: it uses the numeric ranges the task declares if it declares any (the built-in benchmarks do), otherwise it learns them from the run's own evaluated params once ~4 experiments share a numeric key. Until then it just delegates to the normal Researcher |
| `surrogate_explore` | `LOOPLAB_SURROGATE_EXPLORE` | `0.1` | UCB-style exploration weight |
| `researcher_panel` | `LOOPLAB_RESEARCHER_PANEL` | `1` | Generate K ideas, keep the best by an empirical surrogate (1 = off) |
| `foresight` | `LOOPLAB_FORESIGHT` | `true` | FOREAGENT predict-before-execute: LLM world model ranks candidates/ideas before an eval, primed with a data report + memory (master switch) |
| `foresight_panel` | `LOOPLAB_FORESIGHT_PANEL` | `2` | Generate K ideas, keep the one predicted best pre-execution (ranks structural/text ideas the numeric surrogate can't; LLM backend only; 1 = off) |
| `foresight_agentic` | `LOOPLAB_FORESIGHT_AGENTIC` | `true` | Run foresight ranking as a TOOL-USING loop that can pull actual experiment results / data facts before deciding (vs a one-shot prediction). A few extra LLM calls per proposal; falls back to one-shot on any hiccup |
| `foresight_min_confidence` | `LOOPLAB_FORESIGHT_MIN_CONFIDENCE` | `0.0` | Minimum predicted confidence at which a predict-before-execute pick is ACTED on. Below it the ranker abstains (K-idea panel → first proposal; best-of-N → D10 tie-break) instead of committing a low-confidence choice. `0.0` = off (act on every pick); raise toward ~0.5 to make the world model defer when unsure. Bounded to 0.0-1.0 — an out-of-range value is rejected at construction rather than becoming a gate no score can clear. Pairs with the foresight track record the predictor is primed with |
| `foresight_verify` | `LOOPLAB_FORESIGHT_VERIFY` | `true` | PART IV Phase 2c. Replace the world model's SELF-REPORTED confidence (measured Pearson≈0 with realized outcome) with a CALIBRATED §12-verifier score: after the K-idea ranker picks the predicted-best candidate, the grounded + repeated + criteria-decomposed verifier (`foresight_criteria` — likely to improve the objective, and sound/feasible) scores it, and that becomes the confidence the `foresight_min_confidence` gate and telemetry (`confidence_source`) use. Degrades to the self-reported confidence without a client or on any verifier error. A few extra LLM calls per acted-on proposal |
| `foresight_verify_samples` | `LOOPLAB_FORESIGHT_VERIFY_SAMPLES` | `3` | Verifier sample count for `foresight_verify` (the §12 repeated-sampling expectation on a no-logprob backend). `3` tames single-shot variance; `1` = cheaper/noisier. Valid range: `1..8`; a value outside it is **clamped** into the range rather than rejected (`core/config.py` — deliberate, so a resumed run whose snapshot carries an out-of-range value still loads), and direct library calls are bounded independently. |
| `proxy_scoring` | `LOOPLAB_PROXY_SCORING` | `false` | Rank a candidate's potential from early signals |
| `proxy_kill_fraction` | `LOOPLAB_PROXY_KILL_FRACTION` | `0.0` | Skip a full eval for the doomed bottom fraction (0 = off) |
| `novelty_mode` | `LOOPLAB_NOVELTY_MODE` | `llm` | How a proposal is dedup-checked: `off` (Researcher's own judgment) / `algo` (param-distance + optional embedding) / `llm` (an LLM reads the real experiments and decides, then re-proposes — one extra call/proposal) |
| `novelty_gate` | `LOOPLAB_NOVELTY_GATE` | `false` | Reject near-duplicate proposals (param-space distance) |
| `novelty_epsilon` | `LOOPLAB_NOVELTY_EPSILON` | `0.05` | Duplicate threshold for the novelty gate |
| `novelty_semantic` | `LOOPLAB_NOVELTY_SEMANTIC` | `false` | Also reject a proposal whose idea TEXT (rationale+hypothesis) embeds within `novelty_semantic_threshold` cosine of an existing node's — dedups structural/free-form ideas the numeric distance can't. Active whenever the deterministic gate runs — `novelty_mode=algo`, `novelty_gate=true` (legacy alias for algo), or the Strategist novelty stance is `explore`; a no-op only under `novelty_mode=llm`/`off` with a non-explore stance |
| `novelty_semantic_threshold` | `LOOPLAB_NOVELTY_SEMANTIC_THRESHOLD` | `0.92` | Cosine at/above which two idea texts count as duplicates |

## Operators & refinement

| Setting | Env | Default | Description |
|---|---|---|---|
| `ablate_every` | `LOOPLAB_ABLATE_EVERY` | `0` | Ablation-driven refinement every N improves (0 = off; greedy only) |
| `ablate_code_blocks` | `LOOPLAB_ABLATE_CODE_BLOCKS` | `false` | Treat each pipeline code block as an ablation unit (MLE-STAR) |
| `merge_mode` | `LOOPLAB_MERGE_MODE` | `auto` | `auto` (ensemble when the Developer writes code, else mean) · `mean` (param mean) · `ensemble` (code recombination) |
| `complexity_cue` | `LOOPLAB_COMPLEXITY_CUE` | `false` | Inject a complexity hint keyed on the node's child count |
| `feature_engineering` | `LOOPLAB_FEATURE_ENGINEERING` | `false` | Instruct the agent to add engineered features (CAAFE-style; CV gate enforced) |
| `best_of_n` | `LOOPLAB_BEST_OF_N` | `1` | Generate N implementations per node, keep the best by execution-free reward (1 = off) |
| `best_of_n_listwise` | `LOOPLAB_BEST_OF_N_LISTWISE` | `true` | Break a best-of-N static-score tie with a comparative LLM selection (D10) |
| `operator_bandit` | `LOOPLAB_OPERATOR_BANDIT` | `False` | P4: replace the fixed merge/ablate cadences with a UCB bandit over per-operator yield (Δmetric per eval-second). Off by default; `thorough` turns it on |

## Repair & resilience

| Setting | Env | Default | Description |
|---|---|---|---|
| `inline_repair` | `LOOPLAB_INLINE_REPAIR` | `true` | Repair mechanical crashes in place within the same eval (no extra node) |
| `inline_repair_attempts` | `LOOPLAB_INLINE_REPAIR_ATTEMPTS` | `12` | **Hard upper limit** on in-place repair attempts per node — the backstop, not the primary stopping rule. **The crash-triage model decides when to stop**: it is consulted once per attempt (the call the loop already made) and is handed this node's whole repair history — what failed, what each fix claimed it would do, which files it actually touched, how far the pipeline got — and its `abandon` verdict means "I no longer know how to fix this". This cap exists for when that judge is wrong in the expensive direction. **It is charged against the durable `node_repaired` events**, not a process counter, so `looplab resume` (and the operator-resumed pause below) continues a node's repair chain instead of granting it a fresh budget — the judge is likewise shown the repairs made by earlier processes. *Why 12:* the longest legitimate chain on record needed **8** — six PyTorch-Lightning/transformers/accelerate migrations of a repo whose pinned deps were a year stale, and only then two repairs on its actual research question (a DDP `find_unused_parameters` modelling decision); driving that recorded sequence through the real loop, the node reaches its research question and produces a metric at any cap ≥ 8 and dies at the migrations below that. 12 is that 8 plus room for a chain half again as long. **0 = no operator cap** — what a pre-existing run resumes with (`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`), and what 38 of the 46 preserved run directories carry. It no longer means *unbounded*: behind it sits the engine's own absolute ceiling of **50** repairs per node (`engine/evaluate.py::_UNLIMITED_REPAIR_CEILING`), and the terminal says which of the two stopped the node. Unbounded was measured to be worth exactly nothing — with an always-`repair` judge under `0`, one node ran **795 repairs / 796 full evals in 45 s with no terminal**, i.e. the 2345-repair incident was still not prevented on its own snapshot. 50 rather than 12 because an operator who chose `0` must not silently acquire the shipped cap mid-run: it is four times that default and six times the longest legitimate chain on record. A judge that cannot answer is never read as permission to continue, and the two ways it can fail differ: an unreachable endpoint (`unanswerable`) stops the node with `developer_crash` **and** raises one run-level pause naming the provider, which `looplab resume` picks up once it is back, while a live model answering something outside the vocabulary (`unreadable`) stops only that node. Either way it is re-asked once first |
| `inline_repair_reasons` | `LOOPLAB_INLINE_REPAIR_REASONS` | `["crash","timeout","oom"]` | Which failure reasons are eligible for inline repair |
| `deep_repair` | `LOOPLAB_DEEP_REPAIR` | `true` | Hand the Developer a failure taxonomy + "reproduce then fix" directive on debug |
| `auto_install_deps` | `LOOPLAB_AUTO_INSTALL_DEPS` | `true` | Pip-install a **known** missing library and re-run (trusted_local only). "Known" means a fixed 61-entry allow-list of mainstream DS/ML packages in `looplab/runtime/deps.py` (`_PIP_NAME`, checked by `is_installable`); it also carries the import-name→pip-name mismatches (`sklearn`→`scikit-learn`, `cv2`→`opencv-python`, `PIL`→`Pillow`, `faiss`→`faiss-cpu`, …). It covers the classic stack (numpy/pandas/polars/pyarrow/scipy/statsmodels/networkx), gradient boosting (xgboost/lightgbm/catboost), tuning (optuna/hyperopt/bayes_opt/shap), deep learning (torch + vision/audio, lightning, timm, einops, transformers, tensorflow/keras, jax/flax, fastai), retrieval/embeddings (sentence_transformers, faiss, datasets, evaluate, accelerate), experiment loggers (tensorboard/tensorboardX — instrumentation the trainer imports, so an absent one costs a repair attempt or, worse, gets logging stripped out along with the live ASHA/train-monitor curves), NLP (nltk, spacy, gensim, sentencepiece, textblob) and misc DS (hdbscan, tslearn, prophet, mlxtend, tqdm, joblib, numba). A name that is **not** on the list is treated as a code bug (a typo or a missing local module), not an install — so an eval dying on `No module named X` for an off-list `X` will never be repaired by pip. Read `_PIP_NAME` for the exact current list. The traceback does not always **name** the missing library: a library may degrade an absent optional dependency into something that is not an import error at all. Live example — `transformers` guards `init_empty_weights` behind `is_accelerate_available()`, so an absent `accelerate` (already on the allow-list) surfaced as a bare `NameError: name 'init_empty_weights' is not defined` with the word "accelerate" nowhere in the exception, and two repair attempts went on hand-patching the symbol. The crash-triage agent's own diagnosis is therefore admissible as the SOURCE of the name (its structured `missing_dependency` field, or a package the traceback and its rationale name independently), and the engine installs and re-runs without spending a repair attempt. **Fail-closed** (`runtime/deps.py::triage_install_candidates`): the traceback must be unresolved-name shaped (a shape mismatch or an OOM never installs anything, whatever the triage says), the triage must demonstrably be describing *that* traceback, the name must be on the allow-list, and the distribution must be provably ABSENT from the eval interpreter — so naming an installed package installs nothing. Finally, a missing **submodule** of an INSTALLED distribution is not a missing distribution: `No module named 'pytorch_lightning.utilities.cloud_io'` reduces to the top-level `pytorch_lightning`, which pip installs as a unit, so on a box that already has it the install is a no-op recorded as a success — one env-prep round and one full re-eval spent on the byte-identical traceback (live: `runs/rubert-dr-0807` node 0, "Requirement already satisfied", 2.19 s). A **dotted-only** missing name is therefore probed for absence exactly as the triage path is, and left to code repair when the distribution is present; a bare `No module named 'torch'` is direct evidence of absence and is installed with no probe, as before. **This setting now governs two distinct mechanisms, and they are not the same thing.** (a) The *crash-time* installer described above — traceback-driven, per node, and the residue-picker for whatever the declaration below did not cover. (b) The *run-start declarative install*: when a repo task's first editable repo ships a `requirements.txt`, LoopLab derives `python -m pip install -r requirements.txt` and runs it **once per run**, in that repo's source dir, with **no operator configuration** — the operator should not have to know their repo has a requirements file. An explicit `RepoTask.eval.run_setup` **wins entirely** (not prepended, not merged): a prepend would double-install for the operator whose command already is that, and an operator who curated an environment against the repo's pins would have no way left to say "do not install these". A failed declarative install **aborts the run**, exactly as a failed `run_setup` does — with **one deliberate exception, which the owner may overrule**. If pip fails naming *specific requirements it could not resolve*, the install is retried **once** without exactly those lines, and each is recorded per line on `run_setup_finished.dropped_requirements` (the declared line, plus pip's own sentence, verbatim); the receipt action becomes `installed_partial`. The reason: a real repo's requirements file carries lines no reachable index serves — a private package, a withdrawn release, an internal mirror nobody configured — and *"one declared line has no distribution anywhere"* is a different failure from *"the install could not run"*. Only the second is a reason to refuse to start, and one stale line should not be able to stop the lab. This is **not** a silent degrade: if a dropped package really is needed, the run still fails at the point of use with a traceback naming it, and the run-start record explains exactly why it was absent. **Everything else stays fatal**: a pip that crashed, a missing or dead index, any non-zero exit with no per-requirement reason, a timeout, an unresolvable *transitive* dependency (the declaration never named it, so dropping whichever line pulled it in would be a guess), a declaration whose directives name relative paths (`-r`/`-c`/`-e`/`-f` — a reduced copy would resolve them elsewhere), dropping *every* declaration, and the reduced set failing in its turn. An **operator's own** `run_setup` is never rewritten: it runs exactly as written or not at all. Setting `auto_install_deps=false` is how an operator says "LoopLab may not change my environment" and turns off *both*. On a non-`trusted_local` tier the declaration is detected and **refused out loud** (`deps_declared.action == "refused_untrusted_tier"`) rather than silently skipped — the Docker tiers run `--network none`. Only `requirements.txt` is acted on; a `pyproject.toml` / `environment.yml` / `Pipfile` / lockfile is recorded under `deps_declared.observed` and never executed, because installing them means *build this repo as a package* or *hand a solver the shared env*, which are different acts with a blast radius nobody asked for. Because the declaration is read, the crash-time installer is no longer pin-blind: when the repo declares the distribution it is about to install, pip gets the declared line **verbatim** (`pytorch_lightning==1.5.1`) instead of a bare name — the defect that moved a live run's interpreter to Lightning 2.6.5 and cost it 7 of 12 repair attempts. (Not `-c requirements.txt`: pip refuses a constraints file containing extras, which the live testbed's own file has.) **Honouring a pin can DOWNGRADE the shared interpreter**; that is recorded, never inferred — `run_setup_finished.env_delta` carries the before/after version of every declared distribution, and `deps_declared` carries what was detected and what was done about it (see [Concepts](concepts.md)). If the Developer rewrites the requirements file, the change is re-installed for that node (content-digest gated, so an unchanged file costs one read; bounded by a per-run cap on *distinct* declarations). |
| `dep_install_timeout` | `LOOPLAB_DEP_INSTALL_TIMEOUT` | `900.0` | Per-package install budget (seconds) |
| `localize_faults` | `LOOPLAB_LOCALIZE_FAULTS` | `false` | Rank the source files most relevant to a failure (repo tasks) |
| `failure_reflection` | `LOOPLAB_FAILURE_REFLECTION` | `true` | Feed recent failed branches back into the proposal prompt (LATS-style); selective — only when recent failures exist |
| `watchdog_reflection` | `LOOPLAB_WATCHDOG_REFLECTION` | `true` | Feed recent **live-watchdog** observations (train-monitor health verdicts + ASHA intermediate-rank flags) into the proposal prompt, so the proposer avoids re-proposing a configuration already seen training weakly; selective (only when a recent flag exists). Complements `failure_reflection` — surfaces the advisory flags on nodes that ran to completion (fold-ignored diagnostics the failure reflection never sees) |
| `debug_depth` | `LOOPLAB_DEBUG_DEPTH` | `2` | T10: how many error-feedback repairs a failing lineage gets before it is abandoned |
| `systemic_failure_stop` | `LOOPLAB_SYSTEMIC_FAILURE_STOP` | `3` | Stop the whole RUN when this many DISTINCT nodes have ended failed and **not one has ever produced a metric**. The run-level companion to `inline_repair_attempts`, which bounds ONE node's repairs and says nothing about a run whose every node fails for the same reason. The engine's other no-progress guard cannot cover it either: `node_failed` is a TERMINAL, so a run that only fails resets that guard every turn. Measured on `rubertlite-dr-unified-v2` (2026-08-11): **26 hours and 1,705 provider calls over 6 failed / 0 evaluated nodes**, every failure the same environment defect, re-diagnosed from scratch by a fresh Developer each time. Once ANY node is evaluated the environment, dependencies and data are proven and this is **off for the rest of the run** — a later failure is about that idea, so only that node and its direction stop and the search continues. The terminal names the failure reasons the triage already recorded. Node RESETS (`superseded`) and operator aborts are not counted; a node repaired five times and failed is one failed idea. `3` rather than `1` because a first node can fail on something a Developer really can repair. **0 disables it** |
| `inline_repair_retrain_cap` | `LOOPLAB_INLINE_REPAIR_RETRAIN_CAP` | `2` | Max FULL multi-stage re-runs (re-trains) the inline-repair loop may do before abandoning to the inter-node debug operator. A late-stage fix (e.g. a broken `score` script that didn't touch `train`) reuses the completed train checkpoint and re-runs only from the failed stage — cheap, not counted. The reuse check is **fail-closed**: a full, counted re-train is forced not only when the repair changes earlier-stage code, but whenever reuse can't be *proven* safe — the repair deleted any file, changed a non-`.py` file (config/data inputs are invisible to import reachability), the eval runs under a non-default `cmd.cwd`, an earlier stage is opaque (`python -m`, a shell wrapper), or the failed stage is missing from the post-repair pipeline. Exception: a FIRST-stage failure (no completed earlier-stage work exists to discard) is an ordinary retry bounded by `inline_repair_attempts` and the triage model's stop decision, never this cap. **A stage ROLLBACK is charged to this same cap** — when a repair names an earlier, already-successful stage as the real cause (`rollback_stage` on its `done` emit), the engine re-runs the pipeline from there, which discards completed work exactly as a forced full re-train does; a second counter would let a repair alternate the two and pay neither. A rollback is *additionally* bounded to at most one per suspect stage per node (read back off the `stage_rollback` events, so a resume does not refund it), and is refused outright unless that repair also CHANGED something the suspect stage runs or imports. 0 = unlimited (legacy). It bounds COST, which neither of those two does: they bound how many repairs happen, not what each one costs |

## Strategist & meta-control

| Setting | Env | Default | Description |
|---|---|---|---|
| `strategist_backend` | `LOOPLAB_STRATEGIST_BACKEND` | `agent` | Meta-controller: `off` / `rule` / `llm` (single-shot over aggregate stats) / `agent` (default — tool-using, READS run/data/siblings/KB/memory before deciding) |
| `strategist_every` | `LOOPLAB_STRATEGIST_EVERY` | `3` | Consult cadence (created nodes) |
| `concept_retag_every` | `LOOPLAB_CONCEPT_RETAG_EVERY` | `5` | PART V (F1) concept CLASSIFIER re-tag + consolidation cadence (created nodes), decoupled from `strategist_every`. The LLM concept map is heavier and slower-moving than a strategy consult, so it refreshes on this sparser interval (and paces the `concept_pivot` coverage-snapshot). Researcher-authored `idea.concepts` still fold immediately at node_created — this only paces the classifier-evidence + consolidation refresh, so UI concept freshness is unaffected. Fires at the seed boundary too (at or past `n_seeds`, once) so short runs get one pass. **Was `30` until 2026-08-11**, which is longer than a real run here: after the seed firing there was never a second one, and `rubert-dr-0807` ended 14 nodes with tags on exactly ONE — the reason every concept surface reported `count: 1, best_metric: null`. A pass skips already-tagged nodes and is capped per cadence, so its cost scales with NEW nodes rather than with run length |
| `budget_aware` | `LOOPLAB_BUDGET_AWARE` | `false` | Surface remaining eval-compute budget into the proposal prompt |
| `agent_control` | `LOOPLAB_AGENT_CONTROL` | *(see below)* | Per-setting allow-list of which agent roles may change it at runtime |

`agent_control` maps a setting name → the roles allowed to change it: `strategist` (run-wide
meta-controller), `boss` (run-chat operator-proxy), `researcher` (per-experiment, per-node sizing).
A setting **absent** from the map is normally locked — only a human can change it via the snapshot/UI.
The sole conditional exception is the Strategy-only `card_scoring`: enabling the run-start-pinned
Card selector grants it to the Strategist without changing the default flag-off governance snapshot;
an explicit `card_scoring: []` revokes that grant. This
is **enforced at runtime** (`_agent_may`) at every **agent** seam, so removing a role from a knob
truly locks it — not just a UI hint: the Strategist's whole applied control surface (`policy`,
`policy_params`, `ablate_every`, `merge_mode`, `complexity_cue`, `ablate_code_blocks`, `prefer_sweep`,
`novelty_stance`, `developer`, `fidelity`, `timeout`, `eval_parallel`, `llm_parallel`,
`llm_lane_limits`, `card_scoring`) is gated in
`_apply_strategy`. Old snapshots that have only `max_parallel`/`parallel_build` grants remain valid;
an explicit canonical entry takes precedence, including an empty allow-list that revokes the grant.
A `budget_extend`, by contrast, is a **human control intent** — the boss action-builder can only
emit `add_nodes`, so its resource fields (`max_seconds`, `max_eval_seconds`, `timeout`,
`eval_parallel`, `llm_parallel`, plus legacy aliases) reach the log only from an operator and are
applied after bounded validation. (A human/operator
pin via the UI/snapshot always wins — the matrix governs the autonomous agents, not the human.) The
default grants those resource/search-shape knobs to the agents and keeps provider infrastructure
(`llm_model`, `llm_base_url`, credentials, `trust_mode`, `docker_image`) locked.
The Researcher's `timeout` grant authorizes its per-node request but never authorizes changing
`max_eval_timeout`: that operator-owned hard ceiling clamps the accepted request after governance.
(`fidelity`/`novelty_stance`/`prefer_sweep`/`developer` are governance keys for the strategist's
per-node dials — not 1:1 `Settings` fields, but gated the same way.)

`llm_lane_limits` is likewise a Strategy-only allocation rather than a launch setting. It is a
closed map over `build`, `deep_research`, `novelty_dedup`, `enrichment`, and the fail-safe `engine`
lane. Missing lanes are unbounded inside the shared total; supplied widths are raw durable live
deltas in `0..64`, with `0` settling to one worker (never re-running startup AUTO). The Strategist
may reallocate it when granted; an operator may pin the canonical totals and/or this map through
`set_strategy`, and those exact raw values are re-applied on resume. An explicit
`llm_lane_limits: {}` atomically clears every lane cap; omitting the field retains the current map.

`card_scoring` is a separate Strategy-only, atomic treatment for the (default-on) Card selector:
`{stance: explore|balanced|exploit, novelty_weight: 0..1, coverage_weight: 0..1}`. It ranks only
already-eligible Cards and does not replace the policy. Unknown, partial, non-finite, or out-of-range
maps are rejected as a whole. Card mode grants it to the Strategist implicitly unless the governance
map explicitly overrides/revokes it; in Card mode an operator may pin it through `set_strategy`.

Two of the selector's inputs are deliberately distrusted, because before a Card's node is built they
are written by the candidate itself:

* **Coverage** is computed from the proposal's own `concept_tags`, so a self-minted slug would score
  as unexplored ground. An unverified membership is therefore CAPPED at the neutral `0.5`, not
  replaced by it — an honest claim below the cap passes through unchanged, and only an *upward*
  claim is limited. A complete independent receipt (`classifier` or `operator-edited` provenance)
  lifts the cap and restores the full `0..1` range.
* **Confidence** is the foresight ranker's self-assessment of its own board ordering, measured at
  Pearson≈0 with realized outcome (§21.12). The foresight term is now the ranker's chosen RANK
  alone; confidence remains a tie-break. `CardScoring.confidence_weight` defaults to `0.0`, and
  `0.65` reproduces the historical blend exactly.

  That weight is **not** part of the `card_scoring` treatment above, and therefore not settable by
  the Strategist *or* by an operator's `set_strategy` — `validate_card_scoring` rejects any map
  naming it, whole. It is a code-level knob on the public `card_score(..., scoring=...)` hook, for
  an explicit A/B of the old weighting. The asymmetry is deliberate: an LLM Strategist must not be
  able to hand its own self-report back its majority share of an active selection signal, and the
  one validator serves both callers, so the operator path is closed with it. Re-opening it for
  operators means a second validated path, not a wider `_CARD_SCORING_FIELDS`.

## Evaluation rigor & confirmation

| Setting | Env | Default | Description |
|---|---|---|---|
| `confirm_top_k` | `LOOPLAB_CONFIRM_TOP_K` | `0` | Confirm the top-k under multiple seeds before finishing (0 = off) |
| `confirm_seeds` | `LOOPLAB_CONFIRM_SEEDS` | `0` | Seeds for the confirmation pass |
| `confirm_seed_base` | `LOOPLAB_CONFIRM_SEED_BASE` | `1` | Base offset for the disjoint confirmation seeds (kept away from the selection seeds so confirmation is independent) |
| `seed_mode` | `LOOPLAB_SEED_MODE` | `auto` | RepoTask node-seeding: which files are copied per node — `auto` (git-tracked when the editable is a git repo, else full copy) · `tracked` (code only) · `all` (full recursive copy). The task's `protect` entries are copied on top regardless, so an operator-owned scorer is present even when untracked ([tasks](tasks.md)) |
| `holdout_select` | `LOOPLAB_HOLDOUT_SELECT` | `true` | Re-rank the top candidates on a held-out split before declaring the best, so the winner isn't a lucky fit to the selection metric (no re-training; uses the eval's own holdout) |
| `holdout_top_k` | `LOOPLAB_HOLDOUT_TOP_K` | `3` | How many top candidates the holdout re-ranks |
| `select_verifier` | `LOOPLAB_SELECT_VERIFIER` | `false` | R1-c / Part IV. Break an exact selector tie with calibrated §12-verifier soundness. The producer identifies the one selector-reachable tie set (holdout first when applicable, otherwise confirmed/raw mean), grounds every member on its current realized-evidence digest, and appends one atomic `verifier_group_scored` treatment only when every member has a strict majority of valid samples. Replay rejects stale generations/digests, incomplete/subset groups and contract/sample mismatches; a torn or newly expanded group falls back uniformly to deterministic metric+ID order. Strictly a tie-break — never moves a node across a better metric (§21.7). Opt-in, needs an LLM client and calibration via `verifier.calibrate`. Samples and contract are pinned in `run_started` for resume/replay |
| `verifier_ci_tie` | `LOOPLAB_VERIFIER_CI_TIE` | `false` | R1-d / Part IV (§21.19). Widen `select_verifier` from an exact metric tie to a conservative statistical one: a candidate joins the leader only inside the smaller of the leader's standard error and the pooled standard error of the difference. Candidate noise therefore cannot manufacture a wider band; missing/degenerate confirm-noise data falls back to exact equality. The verifier scores the complete selector-reachable tie set atomically and never crosses a significant metric difference (§21.7). Requires `select_verifier`; off ⇒ exact-tie only. The rule is pinned in `run_started` for replay |
| `select_verifier_samples` | `LOOPLAB_SELECT_VERIFIER_SAMPLES` | `3` | Verifier sample count for `select_verifier` (§12 repeated sampling). `3` tames single-shot variance; `1` is cheaper/noisier. Valid range `1..32`. The selected count and verifier contract version are pinned in `run_started`; a group event is accepted only when every selector-reachable member has a current evidence digest and a strict majority of valid samples |
| `holdout_fraction` | `LOOPLAB_HOLDOUT_FRACTION` | `0.25` | Fraction of the eval reserved as the holdout |
| `archive_resolution` | `LOOPLAB_ARCHIVE_RESOLUTION` | `1.0` | Diversity-archive niche bucket width in parameter space |
| `coverage_context` | `LOOPLAB_COVERAGE_CONTEXT` | `true` | Compute the run's breadth read-model (themes / param-niches / theme entropy / dominant-theme fraction) at the strategist cadence, record it as a `coverage_snapshot` audit event, and feed it into the Strategist's decision context (the narrowing signal). Deterministic; additive context only |
| `concept_pivot` | `LOOPLAB_CONCEPT_PIVOT` | `true` | PART IV Phase 2a live steering. Record a concept-graph coverage + uncovered-region snapshot (`concept_coverage_snapshot`) at the `concept_retag_every` cadence (default 5 — the producer gates on `_should_consult_concepts`, not `strategist_every`), and on an `explore` stance make the Researcher's novelty hint name the exact uncovered regions ("0 coverage in {negatives/external-mining, distillation} — go there") instead of the vague "broaden". The snapshot is built by the LLM agent when a reflect client is wired (universal — works on ANY task, no curated skeleton needed, with per-task LLM-derived importance), falling back to the deterministic heuristic over a curated skeleton otherwise; recorded once per cadence so replay stays deterministic. The snapshot and prompt cue do not rank metrics directly, but the resulting concept evidence feeds `graded_novelty` proposal admission and, when enabled, `capability_expansion`; it can therefore change which candidates reach evaluation and selection |
| `graded_novelty` | `LOOPLAB_GRADED_NOVELTY` | `true` | PART IV Phase 2b (D3). Grade a fresh proposal over the concept graph in the LIVE novelty gate: a level-4 "same direction, DIFFERENT implementation" or level-5 "re-opens a wrongly-abandoned FAILED direction" proposal may pass the flat dedup gate and is recorded as `novelty_graded`. It uses the agentic tagger only with a complete classifier-receipt snapshot; otherwise it uses the curated deterministic graph/heuristic path or defers to the ordinary novelty gate. It changes proposal admission (never best-metric ranking). ON by default in product `Settings`; bare-library `EngineOptions` remains off, and conservative deployments can explicitly pin it false until workload-specific cost/quality validation is complete |
| `fingerprint_universal` | `LOOPLAB_FINGERPRINT_UNIVERSAL` | `true` | PART IV cross-run Step 0 (§21.20). Universal task-fingerprint tokenization: drop the ASCII-only `[a-z0-9]` allowlist on goal keywords (`[^\W_]+`/`.casefold()`, any script) so a non-Latin goal (Russian, CJK, …) is not silently dropped from its cross-run fingerprint and can reach SIMILAR-task priors/lessons/cases. ON by default in the product Settings (ce4a379); the bare-library EngineOptions default stays off, so a run pinned to the library default is byte-identical and won't silently re-key a portfolio mid-flight |
| `cross_run_concepts` | `LOOPLAB_CROSS_RUN_CONCEPTS` | `true` | PART IV cross-run Step 2 (§21.20). At run end write a per-run concept capsule; during `_graded_novelty_precheck`, separately surface overlapping earlier concepts as a `cross_run_prior` audit event. The prior is not fed into the gating grade and never rejects. **D8 research-claim persistence is independent of this flag:** whenever shared `memory_dir` is configured, finalize upserts memo-derived v3 claim rows with task/run/direction identity, run-qualified node references, source URLs and verifier verdict/method/note. Every explicitly processed v3 run records producer input/retained/omitted cardinality, including processed-empty and all-invalid sentinels; this receipt does not prove that every historical portfolio run executed D8. The mutable reader separately quarantines malformed, schema-invalid and unknown-future rows. Either an incomplete producer receipt or quarantined durable row makes claim absence/counts a lower bound and withholds one-sided verdicts. Legacy v0-v2 rows remain readable evidence, but their producer denominator is unknown. This remains a lean evidence contract rather than a complete applicability/comparison receipt, and stored memo text remains untrusted. Effective concept-prior surfacing requires a shared `memory_dir`, `graded_novelty` and concept tags produced through `concept_pivot`; use `fingerprint_universal` consistently for non-Latin portfolios. ON by default in the product Settings (ce4a379); the bare-library EngineOptions default stays off |
| `concept_run_base` | `LOOPLAB_CONCEPT_RUN_BASE` | `true` | PART V (B) run-base + node-delta concept authoring. Once the first evaluated node has authored concepts, the engine seeds `run_base_concepts` from them (a one-shot, idempotent `run_concepts` event). Every new `Idea` emits `concept_mode`: `full` is an exact `concepts` replacement; `delta` applies `concepts_added`/`concepts_removed` vs the run base and actual parent union, including an explicit empty/empty zero delta. Replay normalizes and follows the bounded consolidation chain (at most 16 hops) before set algebra, then materializes effective `node_concepts` topologically; classifier/operator/offline receipts still win for an unchanged Idea. Unsupported concept modes or unresolved delta dependencies fail closed with typed per-node receipts; consolidation cycles and over-limit rename chains also fail closed with corruption-class completeness reasons. `ConceptFrame` is then incomplete/non-authoritative instead of presenting fallback emptiness as honest data. Refreshing repeats the same durable fold; inspect the run's Lab → Events and LoopLab → Knowledge &amp; prompts, and re-tag where appropriate, or fork/replay a corrected run. Old no-mode full/absent events retain their historical meaning, while short-lived non-empty no-mode delta payloads remain readable. Off → the hint asks every node for its full set, though the reader continues to understand durable delta events. ON in product `Settings`; bare-library `EngineOptions` stays off |
| `cross_run_read_tools` | `LOOPLAB_CROSS_RUN_READ_TOOLS` | `true` | PART V §22. Adds read-only `cross_run_prior_attempts` / `cross_run_claims` / `cross_run_atlas` / `cross_run_search` / `cross_run_concept_map` (a caller-visible concept graph: task-family + objective-direction scoped for a bound agent, portfolio-wide only for unbound owner/CLI, with most-explored concepts, is_a paths and co-occurrence edges) to in-house Researcher, Strategist, deep-research, Genesis, the in-house `LLMRepoDeveloper` lesson-role variant, and the owner Assistant (`looplab ui`; portfolio-wide because it is never bound to a single run — the most-exposed consumer); external coding-agent Developer backends do not receive this provider. Autonomous run roles have no cross-run mutation function. The owner Assistant is a separate authority: outside plan mode it also receives approval-gated `ConceptGovernanceTools` for merge/purge/split/clear over the shared taxonomy. Rejected claims are filtered from active projections. Every bound provider applies compatible direction; lessons/capsules allow **exact task OR a strict related-goal fingerprint** (at least two shared bare terms covering half of the smaller term set), while v3 D8 rows store no goal fingerprint and are exact-task-only. D8 producer completeness and lesson/research JSONL read health travel through claim, Atlas and search receipts, so tool output labels retained counts/empty matches as lower bounds whenever either source is partial. Search's hash-vector channel is a lexical proxy, not semantic retrieval. Task facets are advisory metadata reserved for future post-scope ranking: they grant no visibility and currently do not change order. Genesis is bound fail-closed to the operator's goal/direction before a task exists, and Repo Developer binds to its task. An explicitly unbound human/CLI provider remains portfolio-wide. This is an applicability heuristic, not an authorization boundary. Tool output marks stored text untrusted and returns a lean search receipt, but individual tool calls are not durably attributed to a later model turn. ON by default in local single-user product `Settings`; bare-library `EngineOptions` remains off. Deployments that require per-user portfolio ACL/redaction must explicitly disable it until the §22.8 authorization gates exist |
| `cross_run_advisory` | `LOOPLAB_CROSS_RUN_ADVISORY` | `true` | PART IV cross-run Step 5 (§21.20.5). Inject a **claim-count-bounded** context pack plus a lean coverage line into Researcher/Strategist prompts. Both paths exclude the current run and scope their source snapshot; Researcher accepts exact-task or fingerprint-related lessons/capsules and exact-task v3 D8, while Strategist is deliberately exact-task. They apply taxonomy/claim overlays, exclude rejected claims, quote persisted text as untrusted data, and persist a compact `{scope_task, excluded_run, source counts, source-health receipts, snapshot/corpus/render digests}` receipt with the resulting node/strategy event. V3 D8 rows retain verifier evidence plus an exact producer cardinality receipt for each explicitly processed run; legacy rows have an unknown producer denominator, and malformed/schema-invalid/future rows are quarantined by the physical read-health receipt. A partial source remains model-visible and prevents exact zero/absence language. These receipts still lack a frozen portfolio watermark, ComparisonContract, access/redaction policy version and per-evidence family applicability, so this is not the full Atlas derivation contract. It never directly changes best-metric selection, but it does change model context and therefore can affect proposals, latency and token cost. Product `Settings` enable this as an explicit local experimental choice; bare-library `EngineOptions` stays off, and promotion/quality/cost gates remain open. Empty only when stores are empty and their receipts are complete |
| `cross_run_structured_claims` | `LOOPLAB_CROSS_RUN_STRUCTURED_CLAIMS` | `true` | PART IV cross-run §21.20.13 (full CR of the lean fuzzy claim merge). Switch the claim read-model to the SCOPE+POLARITY-safe **structured claim key** (`engine/claim_key.py`): claims from different tasks never merge, opposite polarity ("X helps" vs "X never helps") is surfaced as a CONTRADICTION rather than collapsed, and paraphrase/inflection variants group by exact structured key (no transitive over-merge). Scoped operator governance is task-precise; an intentionally unscoped decision is the portfolio-wide fallback (precedence: exact scope+metric → scope-only → global metric → global). Affects the `cross_run_advisory` context pack; ON by default in the product Settings (ce4a379), bare-library EngineOptions default stays off. Lean stemming/negation — a full subject/comparator parse is a further TODO — so treat cross-task recall as scope-safe, not semantically complete |
| `cross_run_curation` | `LOOPLAB_CROSS_RUN_CURATION` | `true` | PART IV cross-run §22.4. At finalize, when an LLM client is available, the concept and claim stewards review the portfolio and **propose only**: outcomes are durably queued in curation logs for operator review. Finalize never applies an agent proposal and never changes taxonomy, claim maturity or retrieval scope. The scheduled stewards are INDEPENDENT and each runs on its own failure boundary: one raising (an unreachable ledger, a provider error) no longer skips the other, and each records a `finalize_step` receipt — `outcome=completed` or `outcome=unavailable` with a bounded reason. Those receipts are diagnostics: they never gate replay, and a steward failure still never blocks terminal completion. Task faceting has its own default-off finalize switch below because no current runtime consumer uses its output. The on-demand concept/claim/task-facet CLI stewards are proposal-only and remain available independently of that scheduling switch: their deprecated `--apply` inputs fail before model setup or paid inference, and every real invocation requires a stable action id with a durable begun/terminal at-most-once receipt. An unresolved begun receipt is never replayed after a crash. After reviewing the exact proposal, an operator must translate selected operations into typed local `concept-merge` / `concept-split` / `claim-decide` commands or owner HTTP actions. Local `claim-decide` and owner HTTP both require an observed revision, action id, live structured claim UID and exact evidence digest, validated through the same locked writer; concept actions additionally require their concept revisions in HTTP. Every owner read publishes an opaque replacement-sensitive `portfolio_id`; typed governance bodies and paid steward queries must echo it as `expected_portfolio_id`, so a live `memory_dir`/directory replacement conflicts before ledger or provider work even if revision counters match. A configured directory that does not yet exist remains readable as empty, but HTTP mutation against that provisional identity returns `409 portfolio_not_initialized` before storage/provider setup; initialize it and refresh first. Owner HTTP derives actor/time, returns structured 409 conflicts, supports explicit claim clear actions, and requires live canonical merge/purge sources and merge targets (split children may be new provisional entities). Concept receipts carry the validated projection digest. Steward HTTP endpoints reject `apply` and only persist proposals. The storage identity is not a frozen corpus snapshot. Assignment backfill, versioned taxonomy/entity and evidence-family releases, impact preview, ACL/workbench and queryable history are still absent. This portfolio-scoped work runs synchronously during finalization: model calls add latency/token cost and its receipts/proposals change persisted audit output even though governance meaning is not auto-applied. Product `Settings` enable the concept/claim pair as an experimental local choice; the bare-library `EngineOptions` default stays off. Needs an initialized `memory_dir` + an LLM backend |
| `task_facets_finalize` | `LOOPLAB_TASK_FACETS_FINALIZE` | `false` | Schedule the proposal-only task-facet steward as a third synchronous finalize call, behind `cross_run_curation`. **Fresh configurations default off** because task facets currently have a generator, durable proposal/idempotency ledger and manual operator APIs, but no retrieval, ranking, authorization or UI behavior consumer. Turning this flag off does not remove `looplab task-facets`, `task-facets-set`, HTTP/on-demand proposal paths or existing ledger data. Turning it on restores the previous all-three finalize treatment and records the same isolated `finalize_step` outcome before terminal cost roll-up. A snapshot that lacks this newer field resumes with it true, so an in-flight historical treatment is not silently changed; it remains inert if that snapshot's umbrella `cross_run_curation` is false. Needs an initialized `memory_dir` + an LLM backend |
| `cross_run_curation_auto` | `LOOPLAB_CROSS_RUN_CURATION_AUTO` | `false` | **Deprecated compatibility input; it does not auto-apply.** Retained so old environment/config snapshots still load. When `cross_run_curation` is enabled, finalize records the request as `auto_requested` in the proposal audit row but remains fail-closed and performs no governance write. An operator must review the exact proposal and apply selected changes through typed concept/claim CLI or owner HTTP governance. Default off; otherwise inert |
| `concept_tidy` | `LOOPLAB_CONCEPT_TIDY` | `false` | PART IV cross-run §22.4 — **taxonomy RATIFICATION**, the consumer the proposal-only steward above has never had. The steward's merge proposals were durably logged and nothing applied them: the original applier (`apply_concept_curation`) was deleted on 2026-08-03 (`72ae8487`, EM-14) for bypassing the CAS discipline, so a portfolio could accumulate correct, paid-for merges forever without one taking effect. When ON, finalize runs `engine/concept_tidy.py::ratify_concept_merges` immediately after the stewards and applies every still-valid proposed **MERGE** through the same `record_concept_alias` the `concept-merge` CLI and the owner HTTP surface use — append-only, applied at READ time (raw per-run tags are never rewritten), cycle-rejecting, and stamped `by=concept-ratifier/v1` so a ratified edge is never mistaken for a human's. Each decision carries an `action_id` derived from its own semantic payload plus a per-decision `expected_governance_revision`, which is precisely the discipline EM-14 found missing: a repeat is a replay, never a second row, so **an operator's `concept-alias-clear` is permanent — a cleared decision is never re-applied**. Proposed SPLITs and PURGEs are never applied; they are counted in the pass receipt as pending operator work. Costs no inference (the judgement was bought at finalize) and appends **no run events at all**, so replay/resume are untouched. A pass is bounded to 32 merges and writes one audit row to `concept_ratification_log.jsonl`; a poisoned governance ledger fails the pass closed rather than reading as "nothing to do". Run it on demand — or preview it — with `looplab concept-ratify MEMORY_DIR [--dry-run]`, which is the same code path. Default OFF: it is the only route by which an agent decision changes cross-run taxonomy without a human in the moment, even though every decision is individually reversible. Needs an initialized `memory_dir` |
| `capability_expansion` | `LOOPLAB_CAPABILITY_EXPANSION` | `false` | PART IV Phase 2b (D7). With `concept_pivot`, action-space lock-in on an explore stance changes the proposal directive toward new capability/infrastructure. The resulting idea is stamped `operator="expand"` and competes normally under SearchFitness, so yield is measurable; the flag does not itself guarantee a capability was built or helped. Keep opt-in and show the dependency/effective state |

The secondary DAG concept UI follows the same materialization truth as `ConceptFrame`, and — since
2026-08-05 — at the same GRANULARITY. A materialization receipt is keyed by node, so an active entry in
`node_concept_materialization_receipts` withholds THAT experiment's row: its retained IDs stay
display-only on its own card and never drive theme grouping, chip counts, search or graph filters, while
every experiment without a receipt keeps its exact membership and remains filterable. The chip bar
discloses the withheld count (`PARTIAL · N withheld`) and its counts are then a lower bound. Only a
run-SCOPED failure refuses the whole control: a degraded or malformed `run_base_concept_receipt`, or a
`node_concept_materialization_receipts` store that is not a map, still shows `UNAVAILABLE`. Withholding
every tagged row also stays visible, because fail-closed emptiness is not an empty concept set.
`UNAVAILABLE` is shown as an integrity state, never as an empty concept set. Receipts retained only for
tombstoned or aborted nodes do not make the current projection partial.
Before that change the bar collapsed the per-node receipts to one run-wide verdict and dropped every
row with them, so a single unresolvable delta node reported `UNAVAILABLE` over a run whose other
experiments carried exact, complete membership — while `ConceptFrame` served those same memberships as
a `partial` frame, which is what the two surfaces now agree on.

Concept owner HTTP mutations use two concurrency tokens: the per-ledger `expected_revision` and the required
cross-alias/split `expected_governance_revision`. Both are strict non-negative integers. Mutation receipts and
Atlas reads expose the resulting shared governance revision; a stale token returns 409 without appending.

## Trust & security

| Setting | Env | Default | Description |
|---|---|---|---|
| `trust_mode` | `LOOPLAB_TRUST_MODE` | `trusted_local` | Sandbox tier: `trusted_local` (subprocess) · `untrusted` (Docker `--network none`) · `hostile` (Docker `--network none` **+ gVisor** `--runtime runsc`) |
| `docker_image` | `LOOPLAB_DOCKER_IMAGE` | `python:3.12-slim` | Image for the untrusted command-eval tier |
| `sandbox_memory` | `LOOPLAB_SANDBOX_MEMORY` | `4g` | Memory cap for the untrusted/hostile Docker tier (`docker run --memory`). Raise for model-training evals; `""` = unbounded. Ignored by `trusted_local`. |
| `sandbox_cpus` | `LOOPLAB_SANDBOX_CPUS` | _(unset)_ | CPU cap for the untrusted/hostile Docker tier (`docker run --cpus`, e.g. `2`). `""` = unbounded. Ignored by `trusted_local`. |
| `sandbox_memory_local` | `LOOPLAB_SANDBOX_MEMORY_LOCAL` | _(unset)_ | Best-effort host-OOM guard for the `trusted_local` (subprocess) tier: an `RLIMIT_AS` cap on each eval child (e.g. `8g`) so a runaway allocation hits `MemoryError` instead of OOM-killing the host. POSIX only. `""` = off; caps **virtual** memory, so leave it off for CUDA/torch (use the Docker tier's `sandbox_memory` for those). |
| `sandbox_fsize_local` | `LOOPLAB_SANDBOX_FSIZE_LOCAL` | _(unset)_ | Best-effort disk-fill guard for the `trusted_local` tier: an `RLIMIT_FSIZE` cap on the size of any single file an eval child writes (e.g. `2g`), so a runaway gets `SIGXFSZ` instead of filling the host disk. POSIX only. `""` = off; leave it off for tasks that write large model checkpoints. |
| `redact_output` | `LOOPLAB_REDACT_OUTPUT` | `false` | Mask credentials in bounded event/span/UI stdout/stderr tails. Raw node-workdir `setup.log`, stage logs, `eval.log`, code and artifacts are outside this redaction boundary and may contain secrets; protect and retain the run root accordingly |
| `reward_hack_detect` | `LOOPLAB_REWARD_HACK_DETECT` | `false` | Flag suspicious wins (grader access, frozen-file writes) |
| `code_leakage_detect` | `LOOPLAB_CODE_LEAKAGE_DETECT` | `false` | Static code-leakage scan (fit-before-split, fit-on-test) |
| `critic_check` | `LOOPLAB_CRITIC_CHECK` | `false` | Execution-free critic of each solution. Broad critic warnings are advisory; the narrowly detected literal-with-no-computed-assignment `critic:hardcoded_metric` signal is classified as high precision and can gate under `trust_gate=gate|block` |
| `workdir_audit` | `LOOPLAB_WORKDIR_AUDIT` | `true` | Audit each node's workdir for tamper signals (writes to frozen/grader files) feeding the reward-hack monitor |
| `trust_gate` | `LOOPLAB_TRUST_GATE` | `audit` | What a **high-precision** reward-hack/leakage signal (plus `critic:hardcoded_metric`) does: `audit` surfaces only; `gate` excludes the node from best-selection and breeding/confirmation while keeping it feasible for diversity/audit; `block` also marks it infeasible. Broad critic, perfect-score, audit-unavailable, and suspicious-output heuristics remain advisory |
| `eval_trust_mode` | `LOOPLAB_EVAL_TRUST_MODE` | `ratify_freeze` | Trust policy for an agent-authored eval spec (onboarding): `ratify_freeze` / `autonomous` / `ratify_freeze_drift` |
| `require_approval` | `LOOPLAB_REQUIRE_APPROVAL` | `false` | HITL: pause for `approve` before finishing |

See [Concepts → Trust & sandbox](concepts.md#trust-the-sandbox) for what each detector does.

## Knowledge, research & memory

| Setting | Env | Default | Description |
|---|---|---|---|
| `memory_dir` | `LOOPLAB_MEMORY_DIR` | `~/.looplab/memory` | Cross-run memory dir (lessons, cases, meta-notes, skills). **On by default**; set blank to disable cross-run memory |
| `knowledge_dir` | `LOOPLAB_KNOWLEDGE_DIR` | `~/.looplab/knowledge` | Knowledge-base dir (notes + cross-run cases); Researcher gets grep/kb_search/read. **On by default** |
| `embed_model` | `LOOPLAB_EMBED_MODEL` | — | Embedding model for **semantic** `kb_search` / case retrieval (e.g. `nomic-embed-text`). Blank = dependency-free lexical hashing. Offline/misconfigured endpoint degrades back to lexical (never crashes) |
| `embed_base_url` | `LOOPLAB_EMBED_BASE_URL` | — | Endpoint for embeddings if different from the chat model's (blank = reuse `llm_base_url`) |
| `memora` | `LOOPLAB_MEMORA` | `true` | **Harmonic memory** (idea import from Memora): index cases/notes by abstraction + cue anchors, consolidate near-duplicates on write, expand retrieval through anchors. On by default; the abstractor itself is chosen by `memora_llm` (LLM by default, lexical fallback). Set `false` to restore the raw-text index |
| `memora_llm` | `LOOPLAB_MEMORA_LLM` | `true` | Write abstractions with the wired chat model (richer than lexical); results are **cached** by content hash and any endpoint failure degrades to lexical. Set `false` to force the deterministic lexical abstractor (zero LLM calls). No-op unless `memora` is on |
| `memora_cache` | `LOOPLAB_MEMORA_CACHE` | — | JSON path for the LLM-abstraction cache. Blank = derived from `memory_dir` / `knowledge_dir`, else in-memory only. No-op unless `memora_llm` is on |
| `memora_anchors` | `LOOPLAB_MEMORA_ANCHORS` | `6` | Max cue anchors kept per memory |
| `memora_consolidate_threshold` | `LOOPLAB_MEMORA_CONSOLIDATE_THRESHOLD` | `0.86` | Cosine at/above which a new memory is consolidated into an existing entry (0.0–1.0) |
| `skills_dir` | `LOOPLAB_SKILLS_DIR` | — | Dir of root `*.md` skills and recursive `**/SKILL.md` packages the Researcher can list/load. Authoring edits root files through flat CAS/recovery names and shows nested packages read-only; bounded traversal skips symlinks/path escapes and discloses an incomplete scan |
| `prompt_dir` | `LOOPLAB_PROMPT_DIR` | — | Dir of editable, hot-reloaded role-prompt `.md` files — see the [override-key table](llm-and-agents.md#prompt-override-keys-prompt_dir) for every `<key>.md` and who consumes it |
| `researcher_tools` | `LOOPLAB_RESEARCHER_TOOLS` | `true` | Let the Researcher read its own experiments + task data mid-loop |
| `cross_run_tools` | `LOOPLAB_CROSS_RUN_TOOLS` | `true` | Read-only tools over sibling runs (same task, same run-root) |
| `all_runs_tools` | `LOOPLAB_ALL_RUNS_TOOLS` | `true` | Read-only tools (`list_all_runs`, `read_run_code`, `read_run_experiment`) over every run **under this run-root**, across ALL tasks — read/reuse any past experiment's code + result. Scope is the configured run-root (`AllRunsTools(run_dir.parent, …)`), not the machine: runs under another project/checkout are invisible, so absence here is not proof of machine-wide absence |
| `literature_search` | `LOOPLAB_LITERATURE_SEARCH` | `false` | arXiv search tool for the Researcher (network-optional) |
| `web_search` | `LOOPLAB_WEB_SEARCH` | `false` | Web search/fetch for the DeepResearcher (network-optional) |
| `research_verify` | `LOOPLAB_RESEARCH_VERIFY` | `true` | Verify a deep-research memo's claims against their cited evidence before it is recorded (synthesis is the documented weak link). Verdicts ride inside the memo and cannot change this run's nodes/champion; finalize uses an aligned `supported` verdict as the evidence gate for positive D8 cross-run claims, while unavailable, stale, or misaligned evidence remains unverified |
| `deep_research_every` | `LOOPLAB_DEEP_RESEARCH_EVERY` | `0` (START IMMEDIATELY) | Run the Deep-Research stage every N created nodes. **This is the one interval setting whose `0` is not the off switch** — it means "no window at all": the stage is due at the FIRST node and at every node-count after it that has not already been researched, so with `concurrent_research` on the first think overlaps the first evaluation instead of waiting. **Off is `-1`** (any negative), where manual `deep_research` and Strategist-requested research still run — exactly what `0` used to mean before 2026-08-07. `N > 0` is a window N nodes wide, as before. Why the default moved: the cadence counts NODES while the feature is phrased around TIME, and on the three flagship GPU runs (1.5–4 h per node, `deep_research_every=3`, `concurrent_research=true`) it fired **zero** times — the first think was 5–12 hours away, so the one feature built to use the idle reasoning agents during a long training never ran on the workload it exists for. A cadence with no Deep-Research model wired (`backend=toy`) is a no-op rather than a recorded stub memo; a manual or Strategist request still records one, because its gate needs the answer |
| `concurrent_research` | `LOOPLAB_CONCURRENT_RESEARCH` | `true` | Overlap a due research "think" with the GPU-bound eval; the memo is recorded immediately when it finishes — including when the eval finishes first, which never discards a think already at the provider — and its directions become standing hints/open cards that can steer the next proposal |
| `concurrent_research_repeat` | `LOOPLAB_CONCURRENT_RESEARCH_REPEAT` | `true` | Don't idle a long (multi-day) eval: RE-RUN the overlapped research on an adaptive timer for the whole training window instead of once. Self-paced — records only a memo whose content is NEW (identical re-runs skipped) and backs off as the analysis converges. The records never rewrite the current champion, but their hints/open cards deliberately steer later proposals and replay reconstructs that advice. The library default is one-shot |
| `concurrent_research_interval_s` | `LOOPLAB_CONCURRENT_RESEARCH_INTERVAL_S` | `1800.0` | Base seconds between repeated research passes — a FLOOR: the effective pace is `max(this, ~5% of the per-experiment time budget)`, so a two-day eval re-researches ~hourly and a short eval not at all. No-op unless `concurrent_research_repeat` is on |
| `concurrent_research_max_calls` | `LOOPLAB_CONCURRENT_RESEARCH_MAX_CALLS` | `40` | Per-eval-window cap on repeated-research LLM calls (0 = cadence-only). Past it the loop stops calling the LLM (the training-health monitor still runs) |
| `concurrent_consolidate` | `LOOPLAB_CONCURRENT_CONSOLIDATE` | `true` | Dedup/merge pure open belief rows during a long eval. Native executable Card work items are excluded: multiple work items may share one `belief_id`, and merging their action identities is not this cadence's job. With Card queue selection, `hypothesis_merged` changes ownership/readiness, so consolidation is deferred to the joined between-node cadence. Needs `track_hypotheses` + a reflect client; background overlap also needs `concurrent_research`. Library default off |
| `track_hypotheses` | `LOOPLAB_TRACK_HYPOTHESES` | `true` | P1: ask the Researcher to state each experiment's hypothesis, register deep-research directions, and track evidence on the Card work-item board. `belief_id` groups cards that ask the same question; proposal and foresight feeds consume one representative per distinct open, untested belief. Those beliefs can steer later proposals without directly re-ranking evaluated nodes. Also drives AGENTIC paraphrase-merge of pure belief rows (hybrid retrieval + the Researcher decides; `hypothesis_merged` events, applied deterministically in the fold) |
| `reflection_priors` | `LOOPLAB_REFLECTION_PRIORS` | `true` | E4/M2/M3: at run end distill the winner + lessons (incl. negatives) with a task fingerprint — the lessons need no winner, a run whose every experiment crashed reflects over its failures instead; at run start inject exact-task notes + fingerprint-matched lessons from similar runs. No-op until `memory_dir` is set |
| `comparative_lessons` | `LOOPLAB_COMPARATIVE_LESSONS` | `true` | M6: distill credit-assigned lessons from PAIRS (which specific change made a child beat/regress its parent). At run end + mid-run; gated on reflection_priors + memory_dir |
| `lessons_every` | `LOOPLAB_LESSONS_EVERY` | `4` | M6 live-share: write comparative lessons to the shared store every N created nodes (0 = run-end only) |
| `lessons_refresh_every` | `LOOPLAB_LESSONS_REFRESH_EVERY` | `4` | M6 live-share: re-read the shared lessons store every N nodes so lessons from CONCURRENT runs reach this run (0 = run-start only) |

## Reporting & observability

| Setting | Env | Default | Description |
|---|---|---|---|
| `report_every` | `LOOPLAB_REPORT_EVERY` | `3` | Regenerate the agent-authored run report every N created nodes (0 = off) |
| `trace_llm_io` | `LOOPLAB_TRACE_LLM_IO` | `true` | Capture a bounded, canonicalized, heuristically redacted diagnostic representation of each LLM call's input/output into `spans.jsonl`; the provider sees the original input and the trace is not byte-exact. The permission is bound to the **run's own tracer**, so two runs sharing one process (the UI's Assistant, Genesis, a library caller driving two `Engine`s) keep opposite policies instead of the last one to start deciding for both. ON in product `Settings`; a bare-library `EngineOptions` declares nothing and defers to the process-wide default set by `looplab.core.tracing.set_llm_capture` (off until a CLI run sets it) |
| `digest_char_cap` | `LOOPLAB_DIGEST_CHAR_CAP` | `0` | Cap (chars) on the in-run experiment digest injected into prompts (0 = AUTO — scales with run size at ~60 chars/node, bounded to [1200, 6000]) |

## External-agent governance

When the Developer is delegated to an external coding agent (`developer_backend` ≠ `default`):

| Setting | Env | Default | Description |
|---|---|---|---|
| `validate_agent` | `LOOPLAB_VALIDATE_AGENT` | `true` | Audit each agent output, retry with feedback, then fall back to the task's original in-process Developer (LLM writer, deterministic/template Developer, or repo baseline) |
| `agent_max_retries` | `LOOPLAB_AGENT_MAX_RETRIES` | `1` | Re-prompts of the agent on an invalid result |
| `agent_patch_gate` | `LOOPLAB_AGENT_PATCH_GATE` | `true` | Run the agent in a git worktree; accept only edits inside the surface |
| `agent_surface` | `LOOPLAB_AGENT_SURFACE` | `["*.py"]` | Edit-surface allow-list (globs) |
| `agent_cmd` | `LOOPLAB_AGENT_CMD` | — | Override the agent's launcher/path |

External-only startup never requires or probes an in-process `developer` target merely because a
custom/historical split Strategy could later ask for `llm`. Its profile may set the external model
and endpoint, but must omit `api_key_env`: a LoopLab-managed key cannot reach the secret-scrubbed CLI.
The in-process target is validated and probed only when replacement is actually requested; a failed
check leaves the current external Developer active. Active validation fallbacks and Repo onboarding
remain real startup consumers and are still checked before work begins.

See [LLM & coding agents → External coding agents](llm-and-agents.md#external-coding-agents).
