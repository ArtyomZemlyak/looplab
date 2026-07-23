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
dir. `resume` loads that snapshot, but it is not the sole authority for every effective field:
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
settings:                 # HOW to run it (any Settings field on this page)
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
server-owned curated catalogue with **156 of the 186 direct `Settings` fields in 10 groups**. The default
**Essential** disclosure mode contains 18 high-frequency keys; search spans all 156 catalogued keys.
Uncatalogued fields remain valid through environment/config/CLI inputs and are preserved by sparse Web
writes.

The packaged catalogue format is v1 and the HTTP/editor contract is v2. The schema response includes
Pydantic-derived validation bounds and a semantic revision exposed as a weak ETag. That ETag only revalidates
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
| `max_nodes` | `LOOPLAB_MAX_NODES` | `8` | Candidate (node) budget for the search |
| `max_parallel` | `LOOPLAB_MAX_PARALLEL` | `1` | Legacy raw-config alias for `eval_parallel`. Retained for old files, CLI arguments, environment variables, and snapshots; prefer the canonical name in new configuration and governance. |
| `parallel_build` | `LOOPLAB_PARALLEL_BUILD` | `1` | Legacy raw-config alias for `llm_parallel`. Retained for old files, CLI arguments, environment variables, and snapshots; prefer the canonical name in new configuration and governance. |
| `eval_parallel` | `LOOPLAB_EVAL_PARALLEL` | _(unset)_ | **Canonical** concurrent EVALUATIONS width inside one Run (GPU/experiment consumer), independent from LLM concurrency. Unset (`None`) falls back to legacy `max_parallel`; launch-time `0` is AUTO (one experiment per detected GPU, at least one). A live Strategist/operator update of `0` settles to safe serial width `1` instead of re-reading mutable hardware. Separate local GPU-owning Runs in the same OS-user filesystem namespace serialize through a pool-wide lease; other users, containers, and hosts need external admission. |
| `llm_parallel` | `LOOPLAB_LLM_PARALLEL` | _(unset)_ | **Canonical** total concurrent LLM-provider-call budget, independent from eval concurrency; its settled value also controls node-build fan-out. Unset (`None`) falls back to legacy `parallel_build` for build fan-out without enabling a shared total; launch-time `0` is AUTO (build fan-out follows resolved `eval_parallel`, while historical research overlap stays unbounded). A live Strategist/operator update of `0` settles both the total and build width to `1`. A positive canonical value activates the shared multi-lane broker. |
| `train_monitor` | `LOOPLAB_TRAIN_MONITOR` | `true` | Per-eval background observer that tails the live training log while a (long) declared command stage runs. Its alert is fold-ignored and cannot directly change lifecycle, champion selection, or replay; when `watchdog_reflection` is on, the raw diagnostic can still advise a later Researcher prompt and thereby affect future proposals. No-ops without an LLM client or on the solution.py path |
| `train_monitor_interval_s` | `LOOPLAB_TRAIN_MONITOR_INTERVAL_S` | `600.0` | Base monitor tick cadence in seconds; the effective cadence adapts to the per-experiment budget and can only be tightened by this. No-op unless `train_monitor` is on |
| `train_monitor_kill` | `LOOPLAB_TRAIN_MONITOR_KILL` | `false` | Opt-in INTERVENTION: let the monitor tree-kill a training it judges `broken` (diverged / silent CPU fallback / not learning) early. The node then fails normally (`reason=monitor_broken`). Off = observe only |
| `train_monitor_kill_confidence` | `LOOPLAB_TRAIN_MONITOR_KILL_CONFIDENCE` | `0.8` | Minimum verdict confidence (0–1) required before a `broken` verdict triggers an early kill |
| `asha_live` | `LOOPLAB_ASHA_LIVE` | `true` | ASHA live-curve watchdog for command evals using `stdout_json` or `stdout_regex`: read the latest intermediate objective metric and rank it against finished siblings at the same declared resource value. Other metric readers have no live observation path. Advisory (a fold-ignored `asha_rank` diagnostic + span) unless the stricter kill contract below is satisfied; with `watchdog_reflection` on, the raw diagnostic can also advise a later proposal without changing the current champion. Needs at least `asha_live_min_siblings` comparable finished nodes. Library default off |
| `asha_live_kill` | `LOOPLAB_ASHA_LIVE_KILL` | `false` | Opt-in intervention only for `stdout_json` with an explicit `metric.resource_key`: tree-kill a node whose comparable intermediate metric stays below the bar past the grace window (fails it `reason=asha_underperforming`). `stdout_regex` can be ranked but not killed; without a resource key the watchdog remains advisory |
| `asha_live_quantile` | `LOOPLAB_ASHA_LIVE_QUANTILE` | `0.5` | The rank bar sits at this quantile along a WORST→BEST ordering of finished siblings' finals: `0.5` = the median; SMALLER lowers the bar toward the WORST peer so it is more conservative (`0.0` = only stop a node worse than the worst finished peer); LARGER is more aggressive |
| `asha_live_min_siblings` | `LOOPLAB_ASHA_LIVE_MIN_SIBLINGS` | `3` | Minimum finished sibling nodes required before ASHA ranks at all (never acts on too little evidence) |
| `timeout` | `LOOPLAB_TIMEOUT` | `30.0` | Per-evaluation wall-clock limit (seconds) |
| `max_eval_timeout` | `LOOPLAB_MAX_EVAL_TIMEOUT` | `3600.0` | Hard ceiling for a Researcher-authored per-node `eval_timeout`, applied after the `agent_control.timeout` permission gate. The run-wide `timeout` remains the fallback when no permitted override is supplied. The one-hour default admits existing heavy-model requests while remaining below the sandbox's defensive 24-hour subprocess ceiling. |
| `sweep_timeout_mult` | `LOOPLAB_SWEEP_TIMEOUT_MULT` | `8.0` | A sweep node (a grid in one process) gets this × `timeout` |
| `n_seeds` | `LOOPLAB_N_SEEDS` | `3` | Seeds per evaluation / rung-0 width |
| `max_seconds` | `LOOPLAB_MAX_SECONDS` | — | Hard wall-clock ceiling for the whole run |
| `max_eval_seconds` | `LOOPLAB_MAX_EVAL_SECONDS` | — | Hard ceiling on cumulative time *inside* evals (survives resume) |

<!-- CODEX AGENT: runtime currently violates the llm_parallel row for a legacy-only source. Per-source
canonicalize_parallelism_source promotes LOOPLAB_PARALLEL_BUILD into llm_parallel before Engine startup, so
the value is indistinguishable from an explicitly canonical positive setting and activates the shared broker.
The compatibility contract needs either provenance-preserving normalization or revised product semantics plus
tests; until then operators should spell llm_parallel explicitly. -->

## Backend & roles

| Setting | Env | Default | Description |
|---|---|---|---|
| `backend` | `LOOPLAB_BACKEND` | `toy` | `toy` (offline optimizer) or `llm` (live model) |
| `developer_backend` | `LOOPLAB_DEVELOPER_BACKEND` | `default` | `default`, or an external agent: `opencode` / `aider` / `goose` / `continue` |
| `unified_agent` | `LOOPLAB_UNIFIED_AGENT` | `true` | One LLM identity plays Researcher + Developer (+ Strategist) across stages |
| `agent_drives_actions` | `LOOPLAB_AGENT_DRIVES_ACTIONS` | `true` | The agent picks the next macro action within a pure legal-action gate |
| `card_driven_selection` | `LOOPLAB_CARD_DRIVEN_SELECTION` | `false` | Opt in to Card-queue macro-action selection. The value is pinned by `run_started`; when both action flags are enabled, Card selection takes precedence over `agent_drives_actions`. |
| `speculation_depth` | `LOOPLAB_SPECULATION_DEPTH` | `0` | Static live-prefetch-backlog cap: outstanding requests plus committed pending speculative Card builds not already admitted to the current consumer session (`0` = fully off, `1`–`64` = bounded overlap). A positive value takes effect only with `card_driven_selection=true` and a currently valid `speculation_gate_receipt`. Pinned by `run_started`, so resume cannot silently mix execution treatments after a config edit. |
| `speculation_gate_receipt` | `LOOPLAB_SPECULATION_GATE_RECEIPT` | — | Absolute path to a local receipt produced by `looplab speculation-gate` from exactly three fresh depth-0/positive-depth calibration pairs (fixed seeds `0/1/2`) on the effective real GPU. Before admitting positive depth, the engine rechecks the exact scorer-fidelity and quality thresholds, current implementation/environment/seven-field GPU identity, per-node CUDA context/allocation proof, raw event/config/task digests, Greedy policy, deterministic quadratic Toy-task profile, tested depth, `max_nodes` and complete runtime-scope digest. General policies/workloads/depths/budgets remain default-off and unadmitted. The receipt path is intentionally not exposed as a casual Settings-UI field. |

Set `unified_agent` and `agent_drives_actions` both to `false` for the legacy split-role behavior.
These are no-ops unless `backend=llm`.

## LLM endpoint

| Setting | Env | Default | Description |
|---|---|---|---|
| `llm_model` | `LOOPLAB_LLM_MODEL` | `qwen3:8b` | Model id |
| `llm_base_url` | `LOOPLAB_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint (Ollama default) |
| `llm_api_key` | `LOOPLAB_LLM_API_KEY` | — | Secret; never serialized as a value. Local servers ignore it |
| `llm_temperature` | `LOOPLAB_LLM_TEMPERATURE` | `0.6` | Sampling temperature |
| `llm_parser` | `LOOPLAB_LLM_PARSER` | `tool_call` | Structured-output strategy (`tool_call`, with text fallback) |
| `llm_guided_json` | `LOOPLAB_LLM_GUIDED_JSON` | `false` | Use the endpoint's constrained decoding (vLLM/SGLang `guided_json`) |
| `llm_reasoning` | `LOOPLAB_LLM_REASONING` | `high` | Thinking depth: `""` (server default) / `off` / `on` / `low` / `medium` / `high` |
| `llm_reasoning_style` | `LOOPLAB_LLM_REASONING_STYLE` | `auto` | How to shape the request: `auto` / `qwen` / `effort` / `none` |
| `llm_reasoning_extra` | `LOOPLAB_LLM_REASONING_EXTRA` | `{}` | Raw fields merged into the request body (escape hatch) |
| `llm_stream` | `LOOPLAB_LLM_STREAM` | `True` | Stream the response (SSE) and reassemble it — bounds a stalled generation via an idle-guard watchdog; off = one blocking request |
| `llm_timeout` | `LOOPLAB_LLM_TIMEOUT` | `180.0` | Per-request inter-token idle limit (s): a stream with no new token for this long is aborted + retried |
| `llm_header_timeout` | `LOOPLAB_LLM_HEADER_TIMEOUT` | `45.0` | First-byte (response-headers) window (s) for **streaming attempts only** before a request is treated as stalled; clamped to `llm_timeout`. Non-stream attempts are not bounded by it |
| `llm_trust_env` | `LOOPLAB_LLM_TRUST_ENV` | `False` | Honor HTTP(S)_PROXY / NO_PROXY env for the LLM client. Default false = a direct connection (the internal endpoint needs no proxy) |
| `llm_cache` | `LOOPLAB_LLM_CACHE` | `False` | Serve identical **deterministic** (temperature-0) LLM requests from an in-process content-addressed cache (cuts cost on retry/panel/verify); sampling calls (temp>0) are never cached. Off by default |
| `compressor_model` | `LOOPLAB_COMPRESSOR_MODEL` | — | Model id for the history auto-summary compressor (blank = the shared chat model) |
| `compressor_base_url` | `LOOPLAB_COMPRESSOR_BASE_URL` | — | Endpoint for the compressor model (blank = reuse llm_base_url) |
| `context_budget_chars` | `LOOPLAB_CONTEXT_BUDGET_CHARS` | `1000000` | Compact the agentic tool-call history once it exceeds this many chars (~250k tokens, sized to the model's context window so reads stay in context); 0 = off |
| `agent_max_turns` | `LOOPLAB_AGENT_MAX_TURNS` | `0` | Max tool-loop turns before the emit is forced; 0 = unlimited (the agent loops until done) |
| `agent_emit_after` | `LOOPLAB_AGENT_EMIT_AFTER` | `300` | Convergence nudge: after N tool turns, prompt the agent once to stop investigating and emit (0 = off) |
| `agent_emit_force` | `LOOPLAB_AGENT_EMIT_FORCE` | `500` | Hard backstop: force the emit at N tool turns so a non-committing model can't burn the whole budget (0 = off) |
| `agent_time_budget_s` | `LOOPLAB_AGENT_TIME_BUDGET_S` | `0` | Wall-clock ceiling across an agent's tool-loop turns; 0 = no cap |
| `agent_stuck_detection` | `LOOPLAB_AGENT_STUCK_DETECTION` | `true` | **B1** — stop an agent that repeats the same call / ping-pongs / re-hits the same error with no progress (forces its emit). The safety net that makes unlimited turns safe |
| `agent_stuck_repeat` | `LOOPLAB_AGENT_STUCK_REPEAT` | `4` | Identical call+result turns in a row that count as "stuck" (min 2) |
| `agent_stuck_alternate` | `LOOPLAB_AGENT_STUCK_ALTERNATE` | `4` | Ping-pong cycles between two calls that count as "stuck" (min 2) |
| `agent_self_plan` | `LOOPLAB_AGENT_SELF_PLAN` | `true` | **C1** — expose a TodoWrite-style `update_plan` tool so a long-running agent keeps its own TODO, re-surfaced periodically |
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
| `researcher_temperature` / `developer_temperature` / `strategist_temperature` | `LOOPLAB_RESEARCHER_TEMPERATURE` / `LOOPLAB_DEVELOPER_TEMPERATURE` / `LOOPLAB_STRATEGIST_TEMPERATURE` | — | Per-role sampling temperature (blank = shared `llm_temperature`). Raise the Researcher for idea breadth, lower the Developer for code determinism. Deep-Research follows the Researcher's value |
| `agent_stage_models` | `LOOPLAB_AGENT_STAGE_MODELS` | `{}` | Unified-agent per-stage model map (`propose`/`implement`/`repair`/`strategy`/`pilot`) |
| `agent_stage_base_urls` | `LOOPLAB_AGENT_STAGE_BASE_URLS` | `{}` | Unified-agent per-stage endpoint map |

See [LLM & coding agents](llm-and-agents.md) for full guidance.

## Search policy & allocation

| Setting | Env | Default | Description |
|---|---|---|---|
| `policy` | `LOOPLAB_POLICY` | `greedy` | `greedy` / `evolutionary` / `mcts` / `asha` |
| `asha_eta` | `LOOPLAB_ASHA_ETA` | `3` | ASHA/BOHB reduction factor (keep top 1/η per rung) |
| `asha_rung_nodes` | `LOOPLAB_ASHA_RUNG_NODES` | `0` | Rung-0 width (0 = use `n_seeds`) |
| `surrogate_proposer` | `LOOPLAB_SURROGATE_PROPOSER` | `false` | BO-lite: propose by a k-NN surrogate over (params→metric) |
| `surrogate_explore` | `LOOPLAB_SURROGATE_EXPLORE` | `0.1` | UCB-style exploration weight |
| `researcher_panel` | `LOOPLAB_RESEARCHER_PANEL` | `1` | Generate K ideas, keep the best by an empirical surrogate (1 = off) |
| `foresight` | `LOOPLAB_FORESIGHT` | `true` | FOREAGENT predict-before-execute: LLM world model ranks candidates/ideas before an eval, primed with a data report + memory (master switch) |
| `foresight_panel` | `LOOPLAB_FORESIGHT_PANEL` | `2` | Generate K ideas, keep the one predicted best pre-execution (ranks structural/text ideas the numeric surrogate can't; LLM backend only; 1 = off) |
| `foresight_agentic` | `LOOPLAB_FORESIGHT_AGENTIC` | `true` | Run foresight ranking as a TOOL-USING loop that can pull actual experiment results / data facts before deciding (vs a one-shot prediction). A few extra LLM calls per proposal; falls back to one-shot on any hiccup |
| `foresight_min_confidence` | `LOOPLAB_FORESIGHT_MIN_CONFIDENCE` | `0.0` | Minimum predicted confidence at which a predict-before-execute pick is ACTED on. Below it the ranker abstains (K-idea panel → first proposal; best-of-N → D10 tie-break) instead of committing a low-confidence choice. `0.0` = off (act on every pick); raise toward ~0.5 to make the world model defer when unsure. Pairs with the foresight track record the predictor is primed with |
| `foresight_verify` | `LOOPLAB_FORESIGHT_VERIFY` | `true` | PART IV Phase 2c. Replace the world model's SELF-REPORTED confidence (measured Pearson≈0 with realized outcome) with a CALIBRATED §12-verifier score: after the K-idea ranker picks the predicted-best candidate, the grounded + repeated + criteria-decomposed verifier (`foresight_criteria` — likely to improve the objective, and sound/feasible) scores it, and that becomes the confidence the `foresight_min_confidence` gate and telemetry (`confidence_source`) use. Degrades to the self-reported confidence without a client or on any verifier error. A few extra LLM calls per acted-on proposal |
| `foresight_verify_samples` | `LOOPLAB_FORESIGHT_VERIFY_SAMPLES` | `3` | Verifier sample count for `foresight_verify` (the §12 repeated-sampling expectation on a no-logprob backend). `3` tames single-shot variance; `1` = cheaper/noisier. Valid range: `1..8`; configuration outside that range is rejected, and direct library calls are bounded independently. |
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
| `inline_repair_attempts` | `LOOPLAB_INLINE_REPAIR_ATTEMPTS` | `0` | Max in-place repair attempts per node (**0 = unlimited**, bounded by the anti-stuck guard) |
| `inline_repair_reasons` | `LOOPLAB_INLINE_REPAIR_REASONS` | `["crash","timeout","oom"]` | Which failure reasons are eligible for inline repair |
| `deep_repair` | `LOOPLAB_DEEP_REPAIR` | `true` | Hand the Developer a failure taxonomy + "reproduce then fix" directive on debug |
| `auto_install_deps` | `LOOPLAB_AUTO_INSTALL_DEPS` | `true` | Pip-install a known missing library and re-run (trusted_local only) |
| `dep_install_timeout` | `LOOPLAB_DEP_INSTALL_TIMEOUT` | `900.0` | Per-package install budget (seconds) |
| `localize_faults` | `LOOPLAB_LOCALIZE_FAULTS` | `false` | Rank the source files most relevant to a failure (repo tasks) |
| `failure_reflection` | `LOOPLAB_FAILURE_REFLECTION` | `true` | Feed recent failed branches back into the proposal prompt (LATS-style); selective — only when recent failures exist |
| `watchdog_reflection` | `LOOPLAB_WATCHDOG_REFLECTION` | `true` | Feed recent **live-watchdog** observations (train-monitor health verdicts + ASHA intermediate-rank flags) into the proposal prompt, so the proposer avoids re-proposing a configuration already seen training weakly; selective (only when a recent flag exists). Complements `failure_reflection` — surfaces the advisory flags on nodes that ran to completion (fold-ignored diagnostics the failure reflection never sees) |
| `debug_depth` | `LOOPLAB_DEBUG_DEPTH` | `2` | T10: how many error-feedback repairs a failing lineage gets before it is abandoned |
| `inline_repair_stuck_repeat` | `LOOPLAB_INLINE_REPAIR_STUCK_REPEAT` | `4` | Identical inline-repair failures in a row that count as "stuck" and stop the in-place retry loop |
| `inline_repair_retrain_cap` | `LOOPLAB_INLINE_REPAIR_RETRAIN_CAP` | `2` | Max FULL multi-stage re-runs (re-trains) the inline-repair loop may do before abandoning to the inter-node debug operator. A late-stage fix (e.g. a broken `score` script that didn't touch `train`) reuses the completed train checkpoint and re-runs only from the failed stage — cheap, not counted. The reuse check is **fail-closed**: a full, counted re-train is forced not only when the repair changes earlier-stage code, but whenever reuse can't be *proven* safe — the repair deleted any file, changed a non-`.py` file (config/data inputs are invisible to import reachability), the eval runs under a non-default `cmd.cwd`, an earlier stage is opaque (`python -m`, a shell wrapper), or the failed stage is missing from the post-repair pipeline. Exception: a FIRST-stage failure (no completed earlier-stage work exists to discard) is an ordinary retry bounded by `inline_repair_attempts` + the anti-stuck guard, never this cap. 0 = unlimited (legacy). Bounds cost since the anti-stuck guard is error-signature-, not cost-based |

## Strategist & meta-control

| Setting | Env | Default | Description |
|---|---|---|---|
| `strategist_backend` | `LOOPLAB_STRATEGIST_BACKEND` | `agent` | Meta-controller: `off` / `rule` / `llm` (single-shot over aggregate stats) / `agent` (default — tool-using, READS run/data/siblings/KB/memory before deciding) |
| `strategist_every` | `LOOPLAB_STRATEGIST_EVERY` | `3` | Consult cadence (created nodes) |
| `concept_retag_every` | `LOOPLAB_CONCEPT_RETAG_EVERY` | `30` | PART V (F1) concept CLASSIFIER re-tag + consolidation cadence (created nodes), decoupled from `strategist_every`. The LLM concept map is heavier and slower-moving than a strategy consult, so it refreshes on this sparser interval (and paces the `concept_pivot` coverage-snapshot). Researcher-authored `idea.concepts` still fold immediately at node_created — this only paces the classifier-evidence + consolidation refresh, so UI concept freshness is unaffected. Fires at the seed boundary too so short runs get one pass |
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

`card_scoring` is a separate Strategy-only, atomic treatment for the opt-in Card selector:
`{stance: explore|balanced|exploit, novelty_weight: 0..1, coverage_weight: 0..1}`. It ranks only
already-eligible Cards and does not replace the policy. Unknown, partial, non-finite, or out-of-range
maps are rejected as a whole. Card mode grants it to the Strategist implicitly unless the governance
map explicitly overrides/revokes it; in Card mode an operator may pin it through `set_strategy`.

## Evaluation rigor & confirmation

| Setting | Env | Default | Description |
|---|---|---|---|
| `confirm_top_k` | `LOOPLAB_CONFIRM_TOP_K` | `0` | Confirm the top-k under multiple seeds before finishing (0 = off) |
| `confirm_seeds` | `LOOPLAB_CONFIRM_SEEDS` | `0` | Seeds for the confirmation pass |
| `confirm_seed_base` | `LOOPLAB_CONFIRM_SEED_BASE` | `1` | Base offset for the disjoint confirmation seeds (kept away from the selection seeds so confirmation is independent) |
| `seed_mode` | `LOOPLAB_SEED_MODE` | `auto` | RepoTask node-seeding: which files are copied per node — `auto` (git-tracked when the editable is a git repo, else full copy) · `tracked` (code only) · `all` (full recursive copy) |
| `holdout_select` | `LOOPLAB_HOLDOUT_SELECT` | `true` | Re-rank the top candidates on a held-out split before declaring the best, so the winner isn't a lucky fit to the selection metric (no re-training; uses the eval's own holdout) |
| `holdout_top_k` | `LOOPLAB_HOLDOUT_TOP_K` | `3` | How many top candidates the holdout re-ranks |
| `select_verifier` | `LOOPLAB_SELECT_VERIFIER` | `false` | R1-c / Part IV. Break an exact selector tie with calibrated §12-verifier soundness. The producer identifies the one selector-reachable tie set (holdout first when applicable, otherwise confirmed/raw mean), grounds every member on its current realized-evidence digest, and appends one atomic `verifier_group_scored` treatment only when every member has a strict majority of valid samples. Replay rejects stale generations/digests, incomplete/subset groups and contract/sample mismatches; a torn or newly expanded group falls back uniformly to deterministic metric+ID order. Strictly a tie-break — never moves a node across a better metric (§21.7). Opt-in, needs an LLM client and calibration via `verifier.calibrate`. Samples and contract are pinned in `run_started` for resume/replay |
| `verifier_ci_tie` | `LOOPLAB_VERIFIER_CI_TIE` | `false` | R1-d / Part IV (§21.19). Widen `select_verifier` from an exact metric tie to a conservative statistical one: a candidate joins the leader only inside the smaller of the leader's standard error and the pooled standard error of the difference. Candidate noise therefore cannot manufacture a wider band; missing/degenerate confirm-noise data falls back to exact equality. The verifier scores the complete selector-reachable tie set atomically and never crosses a significant metric difference (§21.7). Requires `select_verifier`; off ⇒ exact-tie only. The rule is pinned in `run_started` for replay |
| `select_verifier_samples` | `LOOPLAB_SELECT_VERIFIER_SAMPLES` | `3` | Verifier sample count for `select_verifier` (§12 repeated sampling). `3` tames single-shot variance; `1` is cheaper/noisier. Valid range `1..32`. The selected count and verifier contract version are pinned in `run_started`; a group event is accepted only when every selector-reachable member has a current evidence digest and a strict majority of valid samples |
| `holdout_fraction` | `LOOPLAB_HOLDOUT_FRACTION` | `0.25` | Fraction of the eval reserved as the holdout |
| `archive_resolution` | `LOOPLAB_ARCHIVE_RESOLUTION` | `1.0` | Diversity-archive niche bucket width in parameter space |
| `coverage_context` | `LOOPLAB_COVERAGE_CONTEXT` | `true` | Compute the run's breadth read-model (themes / param-niches / theme entropy / dominant-theme fraction) at the strategist cadence, record it as a `coverage_snapshot` audit event, and feed it into the Strategist's decision context (the narrowing signal). Deterministic; additive context only |
| `concept_pivot` | `LOOPLAB_CONCEPT_PIVOT` | `true` | PART IV Phase 2a live steering. Record a concept-graph coverage + uncovered-region snapshot (`concept_coverage_snapshot`) at the `concept_retag_every` cadence (default 30 — the producer gates on `_should_consult_concepts`, not `strategist_every`), and on an `explore` stance make the Researcher's novelty hint name the exact uncovered regions ("0 coverage in {negatives/external-mining, distillation} — go there") instead of the vague "broaden". The snapshot is built by the LLM agent when a reflect client is wired (universal — works on ANY task, no curated skeleton needed, with per-task LLM-derived importance), falling back to the deterministic heuristic over a curated skeleton otherwise; recorded once per cadence so replay stays deterministic. The snapshot and prompt cue do not rank metrics directly, but the resulting concept evidence feeds `graded_novelty` proposal admission and, when enabled, `capability_expansion`; it can therefore change which candidates reach evaluation and selection |
| `graded_novelty` | `LOOPLAB_GRADED_NOVELTY` | `true` | PART IV Phase 2b (D3). Grade a fresh proposal over the concept graph in the LIVE novelty gate: a level-4 "same direction, DIFFERENT implementation" or level-5 "re-opens a wrongly-abandoned FAILED direction" proposal may pass the flat dedup gate and is recorded as `novelty_graded`. It uses the agentic tagger only with a complete classifier-receipt snapshot; otherwise it uses the curated deterministic graph/heuristic path or defers to the ordinary novelty gate. It changes proposal admission (never best-metric ranking). ON by default in product `Settings`; bare-library `EngineOptions` remains off, and conservative deployments can explicitly pin it false until workload-specific cost/quality validation is complete |
| `fingerprint_universal` | `LOOPLAB_FINGERPRINT_UNIVERSAL` | `true` | PART IV cross-run Step 0 (§21.20). Universal task-fingerprint tokenization: drop the ASCII-only `[a-z0-9]` allowlist on goal keywords (`[^\W_]+`/`.casefold()`, any script) so a non-Latin goal (Russian, CJK, …) is not silently dropped from its cross-run fingerprint and can reach SIMILAR-task priors/lessons/cases. ON by default in the product Settings (ce4a379); the bare-library EngineOptions default stays off, so a run pinned to the library default is byte-identical and won't silently re-key a portfolio mid-flight |
| `cross_run_concepts` | `LOOPLAB_CROSS_RUN_CONCEPTS` | `true` | PART IV cross-run Step 2 (§21.20). At run end write a per-run concept capsule; during `_graded_novelty_precheck`, separately surface overlapping earlier concepts as a `cross_run_prior` audit event. The prior is not fed into the gating grade and never rejects. **D8 research-claim persistence is independent of this flag:** whenever shared `memory_dir` is configured, finalize upserts memo-derived v3 claim rows with task/run/direction identity, run-qualified node references, source URLs and verifier verdict/method/note. Every explicitly processed v3 run records producer input/retained/omitted cardinality, including processed-empty and all-invalid sentinels; this receipt does not prove that every historical portfolio run executed D8. The mutable reader separately quarantines malformed, schema-invalid and unknown-future rows. Either an incomplete producer receipt or quarantined durable row makes claim absence/counts a lower bound and withholds one-sided verdicts. Legacy v0-v2 rows remain readable evidence, but their producer denominator is unknown. This remains a lean evidence contract rather than a complete applicability/comparison receipt, and stored memo text remains untrusted. Effective concept-prior surfacing requires a shared `memory_dir`, `graded_novelty` and concept tags produced through `concept_pivot`; use `fingerprint_universal` consistently for non-Latin portfolios. ON by default in the product Settings (ce4a379); the bare-library EngineOptions default stays off |
| `concept_run_base` | `LOOPLAB_CONCEPT_RUN_BASE` | `true` | PART V (B) run-base + node-delta concept authoring. Once the first evaluated node has authored concepts, the engine seeds `run_base_concepts` from them (a one-shot, idempotent `run_concepts` event). Every new `Idea` emits `concept_mode`: `full` is an exact `concepts` replacement; `delta` applies `concepts_added`/`concepts_removed` vs the run base and actual parent union, including an explicit empty/empty zero delta. Replay normalizes and follows the bounded consolidation chain (at most 16 hops) before set algebra, then materializes effective `node_concepts` topologically; classifier/operator/offline receipts still win for an unchanged Idea. Unsupported concept modes or unresolved delta dependencies fail closed with typed per-node receipts; consolidation cycles and over-limit rename chains also fail closed with corruption-class completeness reasons. `ConceptFrame` is then incomplete/non-authoritative instead of presenting fallback emptiness as honest data. Refreshing repeats the same durable fold; inspect Lab → Events/Authoring and re-tag where appropriate, or fork/replay a corrected run. Old no-mode full/absent events retain their historical meaning, while short-lived non-empty no-mode delta payloads remain readable. Off → the hint asks every node for its full set, though the reader continues to understand durable delta events. ON in product `Settings`; bare-library `EngineOptions` stays off |
| `cross_run_read_tools` | `LOOPLAB_CROSS_RUN_READ_TOOLS` | `true` | PART V §22. Adds read-only `cross_run_prior_attempts` / `cross_run_claims` / `cross_run_atlas` / `cross_run_search` / `cross_run_concept_map` (a caller-visible concept graph: task-family + objective-direction scoped for a bound agent, portfolio-wide only for unbound owner/CLI, with most-explored concepts, is_a paths and co-occurrence edges) to in-house Researcher, Strategist, deep-research, Genesis, the in-house `LLMRepoDeveloper` lesson-role variant, and the owner Assistant (`looplab ui`; portfolio-wide because it is never bound to a single run — the most-exposed consumer); external coding-agent Developer backends do not receive this provider. Agents have no cross-run mutation function, and rejected claims are filtered from active projections. Every bound provider applies compatible direction; lessons/capsules allow **exact task OR a strict related-goal fingerprint** (at least two shared bare terms covering half of the smaller term set), while v3 D8 rows store no goal fingerprint and are exact-task-only. D8 producer completeness and lesson/research JSONL read health travel through claim, Atlas and search receipts, so tool output labels retained counts/empty matches as lower bounds whenever either source is partial. Search's hash-vector channel is a lexical proxy, not semantic retrieval. Task facets are advisory metadata reserved for future post-scope ranking: they grant no visibility and currently do not change order. Genesis is bound fail-closed to the operator's goal/direction before a task exists, and Repo Developer binds to its task. An explicitly unbound human/CLI provider remains portfolio-wide. This is an applicability heuristic, not an authorization boundary. Tool output marks stored text untrusted and returns a lean search receipt, but individual tool calls are not durably attributed to a later model turn. ON by default in local single-user product `Settings`; bare-library `EngineOptions` remains off. Deployments that require per-user portfolio ACL/redaction must explicitly disable it until the §22.8 authorization gates exist |
| `cross_run_advisory` | `LOOPLAB_CROSS_RUN_ADVISORY` | `true` | PART IV cross-run Step 5 (§21.20.5). Inject a **claim-count-bounded** context pack plus a lean coverage line into Researcher/Strategist prompts. Both paths exclude the current run and scope their source snapshot; Researcher accepts exact-task or fingerprint-related lessons/capsules and exact-task v3 D8, while Strategist is deliberately exact-task. They apply taxonomy/claim overlays, exclude rejected claims, quote persisted text as untrusted data, and persist a compact `{scope_task, excluded_run, source counts, source-health receipts, snapshot/corpus/render digests}` receipt with the resulting node/strategy event. V3 D8 rows retain verifier evidence plus an exact producer cardinality receipt for each explicitly processed run; legacy rows have an unknown producer denominator, and malformed/schema-invalid/future rows are quarantined by the physical read-health receipt. A partial source remains model-visible and prevents exact zero/absence language. These receipts still lack a frozen portfolio watermark, ComparisonContract, access/redaction policy version and per-evidence family applicability, so this is not the full Atlas derivation contract. It never directly changes best-metric selection, but it does change model context and therefore can affect proposals, latency and token cost. Product `Settings` enable this as an explicit local experimental choice; bare-library `EngineOptions` stays off, and promotion/quality/cost gates remain open. Empty only when stores are empty and their receipts are complete |
| `cross_run_structured_claims` | `LOOPLAB_CROSS_RUN_STRUCTURED_CLAIMS` | `true` | PART IV cross-run §21.20.13 (full CR of the lean fuzzy claim merge). Switch the claim read-model to the SCOPE+POLARITY-safe **structured claim key** (`engine/claim_key.py`): claims from different tasks never merge, opposite polarity ("X helps" vs "X never helps") is surfaced as a CONTRADICTION rather than collapsed, and paraphrase/inflection variants group by exact structured key (no transitive over-merge). Scoped operator governance is task-precise; an intentionally unscoped decision is the portfolio-wide fallback (precedence: exact scope+metric → scope-only → global metric → global). Affects the `cross_run_advisory` context pack; ON by default in the product Settings (ce4a379), bare-library EngineOptions default stays off. Lean stemming/negation — a full subject/comparator parse is a further TODO — so treat cross-task recall as scope-safe, not semantically complete |
| `cross_run_curation` | `LOOPLAB_CROSS_RUN_CURATION` | `true` | PART IV cross-run §22.4. At finalize, when an LLM client is available, the concept, claim and task-facet stewards review the portfolio and **propose only**: outcomes are durably queued in curation logs for operator review. Finalize never applies an agent proposal and never changes taxonomy, claim maturity or retrieval scope. The on-demand concept/claim/task-facet CLI stewards are proposal-only too: their deprecated `--apply` inputs fail before model setup or paid inference, and every real invocation requires a stable action id with a durable begun/terminal at-most-once receipt. An unresolved begun receipt is never replayed after a crash. After reviewing the exact proposal, an operator must translate selected operations into typed local `concept-merge` / `concept-split` / `claim-decide` commands or owner HTTP actions. Local `claim-decide` and owner HTTP both require an observed revision, action id, live structured claim UID and exact evidence digest, validated through the same locked writer; concept actions additionally require their concept revisions in HTTP. Every owner read publishes an opaque replacement-sensitive `portfolio_id`; typed governance bodies and paid steward queries must echo it as `expected_portfolio_id`, so a live `memory_dir`/directory replacement conflicts before ledger or provider work even if revision counters match. A configured directory that does not yet exist remains readable as empty, but HTTP mutation against that provisional identity returns `409 portfolio_not_initialized` before storage/provider setup; initialize it and refresh first. Owner HTTP derives actor/time, returns structured 409 conflicts, supports explicit claim clear actions, and requires live canonical merge/purge sources and merge targets (split children may be new provisional entities). Concept receipts carry the validated projection digest. Steward HTTP endpoints reject `apply` and only persist proposals. The storage identity is not a frozen corpus snapshot. Assignment backfill, versioned taxonomy/entity and evidence-family releases, impact preview, ACL/workbench and queryable history are still absent. This portfolio-scoped work runs synchronously during finalization: model calls add latency/token cost and its receipts/proposals change persisted audit output even though governance meaning is not auto-applied. Product `Settings` enable it as an experimental local choice; the bare-library `EngineOptions` default stays off. Needs an initialized `memory_dir` + an LLM backend |
| `cross_run_curation_auto` | `LOOPLAB_CROSS_RUN_CURATION_AUTO` | `false` | **Deprecated compatibility input; it does not auto-apply.** Retained so old environment/config snapshots still load. When `cross_run_curation` is enabled, finalize records the request as `auto_requested` in the proposal audit row but remains fail-closed and performs no governance write. An operator must review the exact proposal and apply selected changes through typed concept/claim CLI or owner HTTP governance. Default off; otherwise inert |
| `capability_expansion` | `LOOPLAB_CAPABILITY_EXPANSION` | `false` | PART IV Phase 2b (D7). With `concept_pivot`, action-space lock-in on an explore stance changes the proposal directive toward new capability/infrastructure. The resulting idea is stamped `operator="expand"` and competes normally under SearchFitness, so yield is measurable; the flag does not itself guarantee a capability was built or helped. Keep opt-in and show the dependency/effective state |

The secondary DAG concept UI follows the same materialization truth as `ConceptFrame`.
`run_base_concept_receipt` or an active entry in `node_concept_materialization_receipts` marks retained
IDs as `PARTIAL`/display-only: they never drive theme grouping, chips, search, or graph filters.
`UNAVAILABLE` is shown as an integrity state, never as an empty concept set. Receipts retained only for
tombstoned or aborted nodes do not make the current projection partial.

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
| `memora_consolidate_threshold` | `LOOPLAB_MEMORA_CONSOLIDATE_THRESHOLD` | `0.86` | Cosine at/above which a new memory is consolidated into an existing entry |
| `skills_dir` | `LOOPLAB_SKILLS_DIR` | — | Dir of `SKILL.md` files the Researcher can list/load |
| `prompt_dir` | `LOOPLAB_PROMPT_DIR` | — | Dir of editable, hot-reloaded role-prompt `.md` files — see the [override-key table](llm-and-agents.md#prompt-override-keys-prompt_dir) for every `<key>.md` and who consumes it |
| `researcher_tools` | `LOOPLAB_RESEARCHER_TOOLS` | `true` | Let the Researcher read its own experiments + task data mid-loop |
| `cross_run_tools` | `LOOPLAB_CROSS_RUN_TOOLS` | `true` | Read-only tools over sibling runs (same task, same run-root) |
| `all_runs_tools` | `LOOPLAB_ALL_RUNS_TOOLS` | `true` | Read-only tools (`list_all_runs`, `read_run_code`, `read_run_experiment`) over EVERY run on the machine, across ALL tasks — read/reuse any past experiment's code + result |
| `literature_search` | `LOOPLAB_LITERATURE_SEARCH` | `false` | arXiv search tool for the Researcher (network-optional) |
| `web_search` | `LOOPLAB_WEB_SEARCH` | `false` | Web search/fetch for the DeepResearcher (network-optional) |
| `research_verify` | `LOOPLAB_RESEARCH_VERIFY` | `true` | Verify a deep-research memo's claims against their cited evidence before it is recorded (synthesis is the documented weak link). Verdicts ride inside the memo and cannot change this run's nodes/champion; finalize uses an aligned `supported` verdict as the evidence gate for positive D8 cross-run claims, while unavailable, stale, or misaligned evidence remains unverified |
| `deep_research_every` | `LOOPLAB_DEEP_RESEARCH_EVERY` | `3` | Run the Deep-Research stage every N created nodes. `0` disables only the automatic cadence; manual and Strategist-requested research still run |
| `concurrent_research` | `LOOPLAB_CONCURRENT_RESEARCH` | `true` | Overlap a due research "think" with the GPU-bound eval; the memo is recorded immediately when it finishes, and its directions become standing hints/open hypotheses that can steer the next proposal |
| `concurrent_research_repeat` | `LOOPLAB_CONCURRENT_RESEARCH_REPEAT` | `true` | Don't idle a long (multi-day) eval: RE-RUN the overlapped research on an adaptive timer for the whole training window instead of once. Self-paced — records only a memo whose content is NEW (identical re-runs skipped) and backs off as the analysis converges. The records never rewrite the current champion, but their hints/open hypotheses deliberately steer later proposals and replay reconstructs that advice. The library default is one-shot |
| `concurrent_research_interval_s` | `LOOPLAB_CONCURRENT_RESEARCH_INTERVAL_S` | `1800.0` | Base seconds between repeated research passes — a FLOOR: the effective pace is `max(this, ~5% of the per-experiment time budget)`, so a two-day eval re-researches ~hourly and a short eval not at all. No-op unless `concurrent_research_repeat` is on |
| `concurrent_research_max_calls` | `LOOPLAB_CONCURRENT_RESEARCH_MAX_CALLS` | `40` | Per-eval-window cap on repeated-research LLM calls (0 = cadence-only). Past it the loop stops calling the LLM (the training-health monitor still runs) |
| `concurrent_consolidate` | `LOOPLAB_CONCURRENT_CONSOLIDATE` | `true` | Dedup/merge the open hypothesis board during a long eval for legacy policy selection. With Card queue selection, `hypothesis_merged` changes Card ownership/readiness, so consolidation is deferred to the joined between-node cadence. Needs `track_hypotheses` + a reflect client; background overlap also needs `concurrent_research`. Library default off |
| `track_hypotheses` | `LOOPLAB_TRACK_HYPOTHESES` | `true` | P1: ask the Researcher to state each experiment's hypothesis, register deep-research directions, and track them to a verdict on the Hypotheses board. Open board items are shown to the Researcher and may be payoff-ordered by foresight, so they can steer later proposals without directly re-ranking evaluated nodes. Also drives AGENTIC paraphrase-merge of the board (hybrid retrieval + the Researcher decides; `hypothesis_merged` events, applied deterministically in the fold) |
| `reflection_priors` | `LOOPLAB_REFLECTION_PRIORS` | `true` | E4/M2/M3: at run end distill the winner + lessons (incl. negatives) with a task fingerprint; at run start inject exact-task notes + fingerprint-matched lessons from similar runs. No-op until `memory_dir` is set |
| `comparative_lessons` | `LOOPLAB_COMPARATIVE_LESSONS` | `True` | M6: distill credit-assigned lessons from PAIRS (which specific change made a child beat/regress its parent). At run end + mid-run; gated on reflection_priors + memory_dir |
| `lessons_every` | `LOOPLAB_LESSONS_EVERY` | `4` | M6 live-share: write comparative lessons to the shared store every N created nodes (0 = run-end only) |
| `lessons_refresh_every` | `LOOPLAB_LESSONS_REFRESH_EVERY` | `4` | M6 live-share: re-read the shared lessons store every N nodes so lessons from CONCURRENT runs reach this run (0 = run-start only) |

## Reporting & observability

| Setting | Env | Default | Description |
|---|---|---|---|
| `report_every` | `LOOPLAB_REPORT_EVERY` | `3` | Regenerate the agent-authored run report every N created nodes (0 = off) |
| `trace_llm_io` | `LOOPLAB_TRACE_LLM_IO` | `true` | Capture a bounded, canonicalized, heuristically redacted diagnostic representation of each LLM call's input/output into `spans.jsonl`; the provider sees the original input and the trace is not byte-exact |
| `digest_char_cap` | `LOOPLAB_DIGEST_CHAR_CAP` | `0` | Cap (chars) on the in-run experiment digest injected into prompts (0 = AUTO — scales with run size at ~60 chars/node, bounded to [1200, 6000]) |

## External-agent governance

When the Developer is delegated to an external coding agent (`developer_backend` ≠ `default`):

| Setting | Env | Default | Description |
|---|---|---|---|
| `validate_agent` | `LOOPLAB_VALIDATE_AGENT` | `true` | Audit each agent output, retry with feedback, fall back to the in-house Developer |
| `agent_max_retries` | `LOOPLAB_AGENT_MAX_RETRIES` | `1` | Re-prompts of the agent on an invalid result |
| `agent_patch_gate` | `LOOPLAB_AGENT_PATCH_GATE` | `true` | Run the agent in a git worktree; accept only edits inside the surface |
| `agent_surface` | `LOOPLAB_AGENT_SURFACE` | `["*.py"]` | Edit-surface allow-list (globs) |
| `agent_cmd` | `LOOPLAB_AGENT_CMD` | — | Override the agent's launcher/path |

See [LLM & coding agents → External coding agents](llm-and-agents.md#external-coding-agents).
