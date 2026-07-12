# Configuration

LoopLab is configured by a single layered `Settings` object (`looplab/core/config.py`). Every field can
be set four ways, in increasing priority:

1. **Default** (shown below).
2. **Environment variable** — uppercase the field name and prefix with `LOOPLAB_`
   (e.g. `max_nodes` → `LOOPLAB_MAX_NODES`). A `.env` file in the working directory is read too.
3. **A config file** — the `settings:` block of a unified YAML/JSON file passed to `looplab run`
   (see below). `looplab init` scaffolds a fully-commented template.
4. **CLI flag** — a named flag for the common knobs, **or** `-s/--set key=value` (repeatable) for
   **any** setting by its exact field name.

The resolved, **secret-masked** settings are written to `config.snapshot.json` in every run dir, so
each run records exactly how it was configured. `resume` reads that snapshot back.

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

A file with no top-level `task:` key is treated as a bare task (the legacy JSON format), so existing
task files keep working. The file is **input only** — the run dir still records canonical JSON
snapshots, so `resume`/`replay` are unchanged. Precedence within one run: `--set`/flags **>** the
file's `settings:` **>** env/`.env` **>** defaults.

---

## Profile (one-word preset)

| Setting | Env | Default | Description |
|---|---|---|---|
| `profile` | `LOOPLAB_PROFILE` | `default` | `default`/`fast` = lean defaults; `thorough` = turn the built quality/trust machinery on |

`profile` is a **named bundle of setting defaults**. The engine ships every intelligence feature
*off* so a toy `looplab run` stays cheap and deterministic; `profile: thorough` turns the
built-and-tested machinery on in one word — multi-seed confirmation (`confirm_top_k=3`,
`confirm_seeds=3`), the reward-hack / leakage / critic monitors **plus**
`trust_gate=gate` (a flagged win can no longer be selected as best), ablation-driven refinement
(`ablate_every=3`), the adaptive operator bandit (`operator_bandit`), and the proposal cues
(`complexity_cue`, `budget_aware`, `failure_reflection`).

A profile is **config-first**: it only fills fields you did *not* set yourself, so any explicit
knob — in the file, on the CLI (`--set`), or via `LOOPLAB_*` — always wins. It deliberately touches
only quality/trust knobs, never spend (`max_nodes`/`max_parallel` stay yours).

```bash
looplab run examples/dataset_task.json --set profile=thorough      # everything trustworthy, on
looplab run examples/dataset_task.json -s profile=thorough -s confirm_top_k=5   # profile, but k=5
```

## Search budget & loop shape

| Setting | Env | Default | Description |
|---|---|---|---|
| `max_nodes` | `LOOPLAB_MAX_NODES` | `8` | Candidate (node) budget for the search |
| `max_parallel` | `LOOPLAB_MAX_PARALLEL` | `1` | Concurrent evaluations. `1` = one experiment at a time (deterministic); raise to fan out |
| `timeout` | `LOOPLAB_TIMEOUT` | `30.0` | Per-evaluation wall-clock limit (seconds) |
| `sweep_timeout_mult` | `LOOPLAB_SWEEP_TIMEOUT_MULT` | `8.0` | A sweep node (a grid in one process) gets this × `timeout` |
| `n_seeds` | `LOOPLAB_N_SEEDS` | `3` | Seeds per evaluation / rung-0 width |
| `max_seconds` | `LOOPLAB_MAX_SECONDS` | — | Hard wall-clock ceiling for the whole run |
| `max_eval_seconds` | `LOOPLAB_MAX_EVAL_SECONDS` | — | Hard ceiling on cumulative time *inside* evals (survives resume) |

## Backend & roles

| Setting | Env | Default | Description |
|---|---|---|---|
| `backend` | `LOOPLAB_BACKEND` | `toy` | `toy` (offline optimizer) or `llm` (live model) |
| `developer_backend` | `LOOPLAB_DEVELOPER_BACKEND` | `default` | `default`, or an external agent: `opencode` / `aider` / `goose` / `continue` |
| `unified_agent` | `LOOPLAB_UNIFIED_AGENT` | `true` | One LLM identity plays Researcher + Developer (+ Strategist) across stages |
| `agent_drives_actions` | `LOOPLAB_AGENT_DRIVES_ACTIONS` | `true` | The agent picks the next macro action within a pure legal-action gate |

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
| `debug_depth` | `LOOPLAB_DEBUG_DEPTH` | `2` | T10: how many error-feedback repairs a failing lineage gets before it is abandoned |
| `inline_repair_stuck_repeat` | `LOOPLAB_INLINE_REPAIR_STUCK_REPEAT` | `4` | Identical inline-repair failures in a row that count as "stuck" and stop the in-place retry loop |
| `inline_repair_retrain_cap` | `LOOPLAB_INLINE_REPAIR_RETRAIN_CAP` | `2` | Max FULL multi-stage re-runs (re-trains) the inline-repair loop may do before abandoning to the inter-node debug operator. A late-stage fix (e.g. a broken `score` script that didn't touch `train`) reuses the completed train checkpoint and re-runs only from the failed stage — cheap, not counted. The reuse check is **fail-closed**: a full, counted re-train is forced not only when the repair changes earlier-stage code, but whenever reuse can't be *proven* safe — the repair deleted any file, changed a non-`.py` file (config/data inputs are invisible to import reachability), the eval runs under a non-default `cmd.cwd`, an earlier stage is opaque (`python -m`, a shell wrapper), or the failed stage is missing from the post-repair pipeline. Exception: a FIRST-stage failure (no completed earlier-stage work exists to discard) is an ordinary retry bounded by `inline_repair_attempts` + the anti-stuck guard, never this cap. 0 = unlimited (legacy). Bounds cost since the anti-stuck guard is error-signature-, not cost-based |

## Strategist & meta-control

| Setting | Env | Default | Description |
|---|---|---|---|
| `strategist_backend` | `LOOPLAB_STRATEGIST_BACKEND` | `agent` | Meta-controller: `off` / `rule` / `llm` (single-shot over aggregate stats) / `agent` (default — tool-using, READS run/data/siblings/KB/memory before deciding) |
| `strategist_every` | `LOOPLAB_STRATEGIST_EVERY` | `3` | Consult cadence (created nodes) |
| `budget_aware` | `LOOPLAB_BUDGET_AWARE` | `false` | Surface remaining eval-compute budget into the proposal prompt |
| `agent_control` | `LOOPLAB_AGENT_CONTROL` | *(see below)* | Per-setting allow-list of which agent roles may change it at runtime |

`agent_control` maps a setting name → the roles allowed to change it: `strategist` (run-wide
meta-controller), `boss` (run-chat operator-proxy), `researcher` (per-experiment, per-node sizing).
A setting **absent** from the map is locked — only a human can change it via the snapshot/UI. This
is **enforced at runtime** (`_agent_may`) at every **agent** seam, so removing a role from a knob
truly locks it — not just a UI hint: the Strategist's whole applied control surface (`policy`,
`ablate_every`, `merge_mode`, `complexity_cue`, `ablate_code_blocks`, `prefer_sweep`,
`novelty_stance`, `developer`, `fidelity`, `timeout`, `max_parallel`) is gated in `_apply_strategy`.
A `budget_extend`, by contrast, is a **human control intent** — the boss action-builder can only
emit `add_nodes`, so its resource fields (`max_seconds`, `max_eval_seconds`, `timeout`,
`max_parallel`) reach the log only from an operator and are applied **as-is**. (A human/operator
pin via the UI/snapshot always wins — the matrix governs the autonomous agents, not the human.) The
default grants those resource/search-shape knobs to the agents and keeps infra (`llm_*`,
`trust_mode`, `docker_image`, api key) locked.
(`fidelity`/`novelty_stance`/`prefer_sweep`/`developer` are governance keys for the strategist's
per-node dials — not 1:1 `Settings` fields, but gated the same way.)

## Evaluation rigor & confirmation

| Setting | Env | Default | Description |
|---|---|---|---|
| `confirm_top_k` | `LOOPLAB_CONFIRM_TOP_K` | `0` | Confirm the top-k under multiple seeds before finishing (0 = off) |
| `confirm_seeds` | `LOOPLAB_CONFIRM_SEEDS` | `0` | Seeds for the confirmation pass |
| `confirm_seed_base` | `LOOPLAB_CONFIRM_SEED_BASE` | `1` | Base offset for the disjoint confirmation seeds (kept away from the selection seeds so confirmation is independent) |
| `seed_mode` | `LOOPLAB_SEED_MODE` | `auto` | RepoTask node-seeding: which files are copied per node — `auto` (git-tracked when the editable is a git repo, else full copy) · `tracked` (code only) · `all` (full recursive copy) |
| `holdout_select` | `LOOPLAB_HOLDOUT_SELECT` | `true` | Re-rank the top candidates on a held-out split before declaring the best, so the winner isn't a lucky fit to the selection metric (no re-training; uses the eval's own holdout) |
| `holdout_top_k` | `LOOPLAB_HOLDOUT_TOP_K` | `3` | How many top candidates the holdout re-ranks |
| `holdout_fraction` | `LOOPLAB_HOLDOUT_FRACTION` | `0.25` | Fraction of the eval reserved as the holdout |
| `archive_resolution` | `LOOPLAB_ARCHIVE_RESOLUTION` | `1.0` | Diversity-archive niche bucket width in parameter space |
| `coverage_context` | `LOOPLAB_COVERAGE_CONTEXT` | `true` | Compute the run's breadth read-model (themes / param-niches / theme entropy / dominant-theme fraction) at the strategist cadence, record it as a `coverage_snapshot` audit event, and feed it into the Strategist's decision context (the narrowing signal). Deterministic; additive context only |

## Trust & security

| Setting | Env | Default | Description |
|---|---|---|---|
| `trust_mode` | `LOOPLAB_TRUST_MODE` | `trusted_local` | Sandbox tier: `trusted_local` (subprocess) · `untrusted` (Docker `--network none`) · `hostile` (Docker `--network none` **+ gVisor** `--runtime runsc`) |
| `docker_image` | `LOOPLAB_DOCKER_IMAGE` | `python:3.12-slim` | Image for the untrusted command-eval tier |
| `sandbox_memory` | `LOOPLAB_SANDBOX_MEMORY` | `4g` | Memory cap for the untrusted/hostile Docker tier (`docker run --memory`). Raise for model-training evals; `""` = unbounded. Ignored by `trusted_local`. |
| `sandbox_cpus` | `LOOPLAB_SANDBOX_CPUS` | _(unset)_ | CPU cap for the untrusted/hostile Docker tier (`docker run --cpus`, e.g. `2`). `""` = unbounded. Ignored by `trusted_local`. |
| `sandbox_memory_local` | `LOOPLAB_SANDBOX_MEMORY_LOCAL` | _(unset)_ | Best-effort host-OOM guard for the `trusted_local` (subprocess) tier: an `RLIMIT_AS` cap on each eval child (e.g. `8g`) so a runaway allocation hits `MemoryError` instead of OOM-killing the host. POSIX only. `""` = off; caps **virtual** memory, so leave it off for CUDA/torch (use the Docker tier's `sandbox_memory` for those). |
| `sandbox_fsize_local` | `LOOPLAB_SANDBOX_FSIZE_LOCAL` | _(unset)_ | Best-effort disk-fill guard for the `trusted_local` tier: an `RLIMIT_FSIZE` cap on the size of any single file an eval child writes (e.g. `2g`), so a runaway gets `SIGXFSZ` instead of filling the host disk. POSIX only. `""` = off; leave it off for tasks that write large model checkpoints. |
| `redact_output` | `LOOPLAB_REDACT_OUTPUT` | `false` | Mask credentials in stdout/stderr before persisting (recommend on for untrusted) |
| `reward_hack_detect` | `LOOPLAB_REWARD_HACK_DETECT` | `false` | Flag suspicious wins (grader access, frozen-file writes) |
| `code_leakage_detect` | `LOOPLAB_CODE_LEAKAGE_DETECT` | `false` | Static code-leakage scan (fit-before-split, fit-on-test) |
| `critic_check` | `LOOPLAB_CRITIC_CHECK` | `false` | Execution-free critic of each solution (always advisory) |
| `workdir_audit` | `LOOPLAB_WORKDIR_AUDIT` | `true` | Audit each node's workdir for tamper signals (writes to frozen/grader files) feeding the reward-hack monitor |
| `trust_gate` | `LOOPLAB_TRUST_GATE` | `audit` | What a reward-hack / leakage flag does to the search: `audit` (surface only) · `gate` (a flagged node can't be selected best **and isn't bred/confirmed from**, but stays *feasible* so it still counts for diversity/audit) · `block` (also mark it fully infeasible). Critic stays advisory in every mode |
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
| `research_verify` | `LOOPLAB_RESEARCH_VERIFY` | `true` | Verify a deep-research memo's claims against their cited evidence before it's recorded (synthesis is the documented weak link); verdicts ride inside the memo, audit-only |
| `deep_research_every` | `LOOPLAB_DEEP_RESEARCH_EVERY` | `3` | Run the Deep-Research stage every N created nodes (0 = off) |
| `concurrent_research` | `LOOPLAB_CONCURRENT_RESEARCH` | `true` | Overlap a due research "think" with the GPU-bound eval; the memo is recorded immediately when it finishes |
| `track_hypotheses` | `LOOPLAB_TRACK_HYPOTHESES` | `true` | P1: ask the Researcher to state each experiment's hypothesis, register deep-research directions, track them to a verdict on the Hypotheses board (audit-only). Also drives AGENTIC paraphrase-merge of the board (hybrid retrieval + the Researcher decides; `hypothesis_merged` events, applied deterministically in the fold) |
| `reflection_priors` | `LOOPLAB_REFLECTION_PRIORS` | `true` | E4/M2/M3: at run end distill the winner + lessons (incl. negatives) with a task fingerprint; at run start inject exact-task notes + fingerprint-matched lessons from similar runs. No-op until `memory_dir` is set |
| `comparative_lessons` | `LOOPLAB_COMPARATIVE_LESSONS` | `True` | M6: distill credit-assigned lessons from PAIRS (which specific change made a child beat/regress its parent). At run end + mid-run; gated on reflection_priors + memory_dir |
| `lessons_every` | `LOOPLAB_LESSONS_EVERY` | `4` | M6 live-share: write comparative lessons to the shared store every N created nodes (0 = run-end only) |
| `lessons_refresh_every` | `LOOPLAB_LESSONS_REFRESH_EVERY` | `4` | M6 live-share: re-read the shared lessons store every N nodes so lessons from CONCURRENT runs reach this run (0 = run-start only) |

## Reporting & observability

| Setting | Env | Default | Description |
|---|---|---|---|
| `report_every` | `LOOPLAB_REPORT_EVERY` | `3` | Regenerate the agent-authored run report every N created nodes (0 = off) |
| `trace_llm_io` | `LOOPLAB_TRACE_LLM_IO` | `true` | Capture each LLM call's prompt + completion into `spans.jsonl` |
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
