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
`confirm_seeds=3`), the novelty gate, the reward-hack / leakage / critic monitors **plus**
`trust_gate=gate` (a flagged win can no longer be selected as best), ablation-driven refinement
(`ablate_every=3`), and the proposal cues (`complexity_cue`, `budget_aware`, `failure_reflection`).

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
| `context_budget_chars` | `LOOPLAB_CONTEXT_BUDGET_CHARS` | `0` | Cap on the agentic tool-call history (chars); 0 = unbounded |
| `agent_max_turns` | `LOOPLAB_AGENT_MAX_TURNS` | `0` | Max tool-loop turns before the emit is forced; 0 = unlimited (the agent loops until done) |
| `agent_time_budget_s` | `LOOPLAB_AGENT_TIME_BUDGET_S` | `0` | Wall-clock ceiling across an agent's tool-loop turns; 0 = no cap |
| `agent_stuck_detection` | `LOOPLAB_AGENT_STUCK_DETECTION` | `true` | **B1** — stop an agent that repeats the same call / ping-pongs / re-hits the same error with no progress (forces its emit). The safety net that makes unlimited turns safe |
| `agent_stuck_repeat` | `LOOPLAB_AGENT_STUCK_REPEAT` | `4` | Identical call+result turns in a row that count as "stuck" (min 2) |
| `agent_stuck_alternate` | `LOOPLAB_AGENT_STUCK_ALTERNATE` | `4` | Ping-pong cycles between two calls that count as "stuck" (min 2) |
| `agent_self_plan` | `LOOPLAB_AGENT_SELF_PLAN` | `true` | **C1** — expose a TodoWrite-style `update_plan` tool so a long-running agent keeps its own TODO, re-surfaced periodically |
| `agent_plan_reinject_every` | `LOOPLAB_AGENT_PLAN_REINJECT_EVERY` | `5` | How often (tool-loop turns) to re-surface the agent's current plan |
| `agent_auto_summary` | `LOOPLAB_AGENT_AUTO_SUMMARY` | `true` | **C2** — LLM-summarize the stale middle of the tool-loop history once it grows long (trigger = `context_budget_chars` if set, else a ~120k-char built-in high-water mark) |

### Per-role / per-stage models

Run the Researcher and Developer on different models or endpoints (e.g. a coder model for the
Developer, a fast model for breadth). Blank values fall back to the shared `llm_*`.

| Setting | Env | Description |
|---|---|---|
| `researcher_model` / `developer_model` | `LOOPLAB_RESEARCHER_MODEL` / `LOOPLAB_DEVELOPER_MODEL` | Per-role model id |
| `researcher_base_url` / `developer_base_url` | `LOOPLAB_RESEARCHER_BASE_URL` / `LOOPLAB_DEVELOPER_BASE_URL` | Per-role endpoint |
| `agent_stage_models` | `LOOPLAB_AGENT_STAGE_MODELS` | Unified-agent per-stage model map (`propose`/`implement`/`repair`/`strategy`/`pilot`) |
| `agent_stage_base_urls` | `LOOPLAB_AGENT_STAGE_BASE_URLS` | Unified-agent per-stage endpoint map |

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
| `proxy_scoring` | `LOOPLAB_PROXY_SCORING` | `false` | Rank a candidate's potential from early signals |
| `proxy_kill_fraction` | `LOOPLAB_PROXY_KILL_FRACTION` | `0.0` | Skip a full eval for the doomed bottom fraction (0 = off) |
| `novelty_gate` | `LOOPLAB_NOVELTY_GATE` | `false` | Reject near-duplicate proposals (param-space distance) |
| `novelty_epsilon` | `LOOPLAB_NOVELTY_EPSILON` | `0.05` | Duplicate threshold for the novelty gate |

## Operators & refinement

| Setting | Env | Default | Description |
|---|---|---|---|
| `ablate_every` | `LOOPLAB_ABLATE_EVERY` | `0` | Ablation-driven refinement every N improves (0 = off; greedy only) |
| `ablate_code_blocks` | `LOOPLAB_ABLATE_CODE_BLOCKS` | `false` | Treat each pipeline code block as an ablation unit (MLE-STAR) |
| `merge_mode` | `LOOPLAB_MERGE_MODE` | `mean` | `mean` (param mean) or `ensemble` (code-recombination ensemble) |
| `complexity_cue` | `LOOPLAB_COMPLEXITY_CUE` | `false` | Inject a complexity hint keyed on the node's child count |
| `feature_engineering` | `LOOPLAB_FEATURE_ENGINEERING` | `false` | Instruct the agent to add engineered features (CAAFE-style; CV gate enforced) |
| `best_of_n` | `LOOPLAB_BEST_OF_N` | `1` | Generate N implementations per node, keep the best by execution-free reward (1 = off) |

## Repair & resilience

| Setting | Env | Default | Description |
|---|---|---|---|
| `inline_repair` | `LOOPLAB_INLINE_REPAIR` | `true` | Repair mechanical crashes in place within the same eval (no extra node) |
| `inline_repair_attempts` | `LOOPLAB_INLINE_REPAIR_ATTEMPTS` | `1` | Max in-place repair attempts per node |
| `inline_repair_reasons` | `LOOPLAB_INLINE_REPAIR_REASONS` | `["crash","timeout"]` | Which failure reasons are eligible for inline repair |
| `deep_repair` | `LOOPLAB_DEEP_REPAIR` | `true` | Hand the Developer a failure taxonomy + "reproduce then fix" directive on debug |
| `auto_install_deps` | `LOOPLAB_AUTO_INSTALL_DEPS` | `true` | Pip-install a known missing library and re-run (trusted_local only) |
| `dep_install_timeout` | `LOOPLAB_DEP_INSTALL_TIMEOUT` | `900.0` | Per-package install budget (seconds) |
| `localize_faults` | `LOOPLAB_LOCALIZE_FAULTS` | `false` | Rank the source files most relevant to a failure (repo tasks) |
| `failure_reflection` | `LOOPLAB_FAILURE_REFLECTION` | `false` | Feed recent failed branches back into the proposal prompt (LATS-style) |

## Strategist & meta-control

| Setting | Env | Default | Description |
|---|---|---|---|
| `strategist_backend` | `LOOPLAB_STRATEGIST_BACKEND` | `llm` | Meta-controller: `off` / `rule` / `llm` |
| `strategist_every` | `LOOPLAB_STRATEGIST_EVERY` | `3` | Consult cadence (created nodes) |
| `budget_aware` | `LOOPLAB_BUDGET_AWARE` | `false` | Surface remaining eval-compute budget into the proposal prompt |
| `agent_control` | `LOOPLAB_AGENT_CONTROL` | *(see below)* | Per-setting allow-list of which agent roles may change it at runtime |

`agent_control` maps a setting name → the roles allowed to change it: `strategist` (run-wide
meta-controller), `boss` (run-chat operator-proxy), `researcher` (per-experiment, per-node sizing).
A setting **absent** from the map is locked — only a human can change it via the snapshot/UI. The
default grants resource/search-shape knobs to the agents and keeps infra (`llm_*`, `trust_mode`,
`docker_image`, api key) locked.

## Evaluation rigor & confirmation

| Setting | Env | Default | Description |
|---|---|---|---|
| `confirm_top_k` | `LOOPLAB_CONFIRM_TOP_K` | `0` | Confirm the top-k under multiple seeds before finishing (0 = off) |
| `confirm_seeds` | `LOOPLAB_CONFIRM_SEEDS` | `0` | Seeds for the confirmation pass |
| `archive_resolution` | `LOOPLAB_ARCHIVE_RESOLUTION` | `1.0` | Diversity-archive niche bucket width in parameter space |

## Trust & security

| Setting | Env | Default | Description |
|---|---|---|---|
| `trust_mode` | `LOOPLAB_TRUST_MODE` | `trusted_local` | Sandbox tier: `trusted_local` (subprocess) or `untrusted` (Docker `--network none`) |
| `docker_image` | `LOOPLAB_DOCKER_IMAGE` | `python:3.12-slim` | Image for the untrusted command-eval tier |
| `redact_output` | `LOOPLAB_REDACT_OUTPUT` | `false` | Mask credentials in stdout/stderr before persisting (recommend on for untrusted) |
| `reward_hack_detect` | `LOOPLAB_REWARD_HACK_DETECT` | `false` | Flag suspicious wins (grader access, frozen-file writes) |
| `code_leakage_detect` | `LOOPLAB_CODE_LEAKAGE_DETECT` | `false` | Static code-leakage scan (fit-before-split, fit-on-test) |
| `critic_check` | `LOOPLAB_CRITIC_CHECK` | `false` | Execution-free critic of each solution (always advisory) |
| `trust_gate` | `LOOPLAB_TRUST_GATE` | `audit` | What a reward-hack / leakage flag does to selection: `audit` (surface only) · `gate` (a flagged node can't be selected best) · `block` (also mark it infeasible so the policy won't breed from it). Critic stays advisory in every mode |
| `eval_trust_mode` | `LOOPLAB_EVAL_TRUST_MODE` | `ratify_freeze` | Trust policy for an agent-authored eval spec (onboarding): `ratify_freeze` / `autonomous` / `ratify_freeze_drift` |
| `require_approval` | `LOOPLAB_REQUIRE_APPROVAL` | `false` | HITL: pause for `approve` before finishing |

See [Concepts → Trust & sandbox](concepts.md#trust--the-sandbox) for what each detector does.

## Knowledge, research & memory

| Setting | Env | Default | Description |
|---|---|---|---|
| `memory_dir` | `LOOPLAB_MEMORY_DIR` | — | Cross-run case library (best result of each run becomes retrievable knowledge) |
| `knowledge_dir` | `LOOPLAB_KNOWLEDGE_DIR` | — | Notes dir; the Researcher gets grep/kb_search/read tools over it |
| `embed_model` | `LOOPLAB_EMBED_MODEL` | — | Embedding model for **semantic** `kb_search` / case retrieval (e.g. `nomic-embed-text`). Blank = dependency-free lexical hashing. Offline/misconfigured endpoint degrades back to lexical (never crashes) |
| `embed_base_url` | `LOOPLAB_EMBED_BASE_URL` | — | Endpoint for embeddings if different from the chat model's (blank = reuse `llm_base_url`) |
| `skills_dir` | `LOOPLAB_SKILLS_DIR` | — | Dir of `SKILL.md` files the Researcher can list/load |
| `prompt_dir` | `LOOPLAB_PROMPT_DIR` | — | Dir of editable, hot-reloaded role-prompt `.md` files |
| `researcher_tools` | `LOOPLAB_RESEARCHER_TOOLS` | `true` | Let the Researcher read its own experiments + task data mid-loop |
| `cross_run_tools` | `LOOPLAB_CROSS_RUN_TOOLS` | `true` | Read-only tools over sibling runs (same task, same run-root) |
| `literature_search` | `LOOPLAB_LITERATURE_SEARCH` | `false` | arXiv search tool for the Researcher (network-optional) |
| `web_search` | `LOOPLAB_WEB_SEARCH` | `false` | Web search/fetch for the DeepResearcher (network-optional) |
| `deep_research_every` | `LOOPLAB_DEEP_RESEARCH_EVERY` | `0` | Run the Deep-Research stage every N created nodes (0 = off) |
| `concurrent_research` | `LOOPLAB_CONCURRENT_RESEARCH` | `false` | Overlap a due research "think" with the GPU-bound eval (remote-LLM win) |
| `track_hypotheses` | `LOOPLAB_TRACK_HYPOTHESES` | `true` | P1: ask the Researcher to state each experiment's hypothesis, register deep-research directions, track them to a verdict on the Hypotheses board (audit-only) |
| `reflection_priors` | `LOOPLAB_REFLECTION_PRIORS` | `true` | E4/M2/M3: at run end distill the winner + lessons (incl. negatives) with a task fingerprint; at run start inject exact-task notes + fingerprint-matched lessons from similar runs. No-op until `memory_dir` is set |

## Reporting & observability

| Setting | Env | Default | Description |
|---|---|---|---|
| `report_every` | `LOOPLAB_REPORT_EVERY` | `3` | Regenerate the agent-authored run report every N created nodes (0 = off) |
| `trace_llm_io` | `LOOPLAB_TRACE_LLM_IO` | `true` | Capture each LLM call's prompt + completion into `spans.jsonl` |

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
