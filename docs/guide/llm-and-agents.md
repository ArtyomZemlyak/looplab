# LLM & coding agents

LoopLab's Researcher and Developer roles are **pluggable backends**. They can be the offline `toy`
optimizer, a live LLM over any OpenAI-compatible endpoint, or — for the Developer — a full external
coding agent. Swapping a backend is a config change; the engine, sandbox, policy, and event log are
unchanged.

## Backends at a glance

| | Offline | Live LLM | External agent |
|---|---|---|---|
| **Set** | `--backend toy` (default) | `--backend llm` | `--developer-backend opencode` |
| **Researcher** | deterministic optimizer | the model | the model |
| **Developer** | templated | the model writes code | the agent edits a worktree |
| **Network** | none | LLM endpoint only | LLM endpoint only |

## Using a live LLM

Point LoopLab at any OpenAI-compatible `/v1` endpoint:

```bash
export LOOPLAB_BACKEND=llm
export LOOPLAB_LLM_BASE_URL=http://localhost:11434/v1     # Ollama
export LOOPLAB_LLM_MODEL=qwen3:8b
# export LOOPLAB_LLM_API_KEY=sk-...                       # hosted endpoints only
```

Verify before a real run:

```bash
looplab smoke        # sends a text + a structured (tool-call) request and reports each
```

Then run with `--backend llm` (or the env var above):

```bash
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
```

### Endpoint options

| Endpoint | `LOOPLAB_LLM_BASE_URL` | Notes |
|---|---|---|
| **Ollama** | `http://localhost:11434/v1` | Native Windows; easiest local start (`ollama pull qwen3:8b`) |
| **vLLM** | `http://host:8000/v1` | Supports constrained decoding (`llm_guided_json`) |
| **SGLang** | `http://host:30000/v1` | Use `--tool-call-parser qwen` for Qwen tool-calls |
| **OpenAI / compatible** | the vendor's `/v1` | Set `LOOPLAB_LLM_API_KEY` |

The client (`OpenAICompatibleClient`) runs on the **openai SDK over an httpx transport** (migrated from the old stdlib-urllib transport for reliable timeouts + a streaming idle-guard); `openai`/`httpx` are declared deps but import-guarded so offline/replay still imports. A LiteLLM client is also available. Structured
output uses tool-calling with an automatic text-parse fallback, so weaker models still work.

## Reasoning / thinking

`llm_reasoning` controls the chain-of-thought sent in the request (defaults to `high` — the agent
reasons before proposing/repairing). The model's thinking is captured either way (a
`reasoning_content` field or inline `<think>`), and the UI can show it per node.

| `LOOPLAB_LLM_REASONING` | Effect |
|---|---|
| `""` | Send nothing (server default) |
| `off` | Actively disable thinking |
| `on` | Enable at default depth |
| `low` / `medium` / `high` | Enable at that effort (default `high`) |

`llm_reasoning_style` shapes how the request param is built (`auto` / `qwen` / `effort` / `none`);
`llm_reasoning_extra` is a raw escape hatch merged into the body. To get a *separate*
`reasoning_content` field from SGLang/vLLM, the server also needs `--reasoning-parser qwen3`.

## Constrained decoding

`LOOPLAB_LLM_GUIDED_JSON=1` drives structured calls with the endpoint's `guided_json` /
`response_format` (vLLM/SGLang) so a weak model can't emit invalid JSON. Off by default (and for
Ollama). Turn it on only if a model struggles to produce valid structured output.

## Per-role and per-stage models

Run the Researcher and Developer on different models or endpoints — e.g. a strong coder model for
the Developer and a fast model for breadth:

```bash
export LOOPLAB_RESEARCHER_MODEL=qwen3:8b
export LOOPLAB_DEVELOPER_MODEL=qwen3-coder:30b
export LOOPLAB_DEVELOPER_BASE_URL=http://coder-host:8000/v1
```

Blank values fall back to the shared `llm_model` / `llm_base_url`. With the **unified agent** (one
identity across stages, on by default), use `agent_stage_models` / `agent_stage_base_urls` to
override per stage — recognized keys are `propose`, `implement`, `repair`, `strategy`, `pilot`:

```bash
export LOOPLAB_AGENT_STAGE_MODELS='{"implement":"qwen3-coder:30b","repair":"qwen3-coder:30b"}'
```

## External coding agents

The Developer role can be delegated to an external terminal coding agent. LoopLab runs it headless
in a git worktree, points it at your local LLM endpoint, and reads the edited solution back.

```bash
looplab run examples/code_regression_task.json \
    --backend llm --developer-backend opencode --model qwen3:8b
```

Supported presets: **`opencode`**, **`aider`**, **`goose`**, **`continue`**. Three guardrails make
this robust (all on by default):

- **Self-contained & headless.** A config (e.g. `opencode.json` with a local Ollama provider and an
  explicit `--model`) is dropped into the agent workdir, so the agent never fetches an external
  model registry on startup.
- **Output validation** (`validate_agent`, `agent_max_retries`). Every agent output is checked
  (launched / not-timed-out / produced / modified-seed / parses / in-surface). On failure it
  re-prompts the agent with the reason, then falls back to the in-house LLM Developer. Each node
  logs an `agent_validated` event.
- **Patch-gated, multi-file** (`agent_patch_gate`, `agent_surface`). The agent runs in a git
  worktree; its diff is gated by an edit-surface allow-list (default `*.py`, reject-not-strip).
  Accepted files become `Node.files` (files-as-truth, resumable) and are materialized into the eval
  workdir.

| Setting | Default | Purpose |
|---|---|---|
| `developer_backend` | `default` | `default` / `opencode` / `aider` / `goose` / `continue` |
| `agent_cmd` | — | Override the launcher/path |
| `validate_agent` | `true` | Audit + retry + fall back |
| `agent_max_retries` | `1` | Re-prompts on an invalid result |
| `agent_patch_gate` | `true` | Worktree + surface-gated diff |
| `agent_surface` | `["*.py"]` | Edit-surface globs |

The built-in LLM Developer (writes code via your endpoint, no external fetch) remains the
zero-dependency default coding path.

## The Developer: three phases (stages → plan → implement)

On a fresh (non-repair) repo node the Developer runs **three separately-traced phases**, so the context
stays focused and the trace reads cleanly:

1. **STAGES** (mandatory, first — unless the operator pre-empts it) — a **read-only** phase whose only
   exit is `declare_stages`. The Developer studies the repo + the operator's `cmd` and declares the
   ordered eval pipeline (e.g. `data_prep → train → …`) that runs BEFORE the operator's protected
   `score` step, baking this node's hyperparameters into the train command. It writes
   `looplab_stages.json`. The **Developer** owns the stages (it knows the repo), not the
   planner/Genesis. Two operator knobs skip the phase: a valid `cmd.stages` pipeline (the engine runs
   it verbatim; the Developer implements the code those stages run) or a protected
   `looplab_stages.json` (disables Developer pipelines — no point burning an LLM loop whose manifest
   would be dropped). If `cmd` is present it's shown as immutable (declare only the preceding stages);
   if absent, declare the full pipeline including a final evaluating stage. The name `score` is
   **reserved either way** (it always denotes the engine-appended operator step). Good practice:
   separate **prep / train / test** stages.
2. **PLAN** — decomposes the code changes into ordered atomic steps (still read-only).
3. **IMPLEMENT** — writes the code those stages run, one bounded session per step. The prompt states
   the node's **actual** declared pipeline (or its absence), and the session carries `declare_stages`
   only to **fix** a broken manifest (e.g. a repair whose root cause is a bad stage command/timeout) —
   authoring stays in the STAGES phase.

A **repair** skips stages+plan and is one focused session. The first **two** phases are read-only: they
get the repo scouts plus the env inspector (`read_file`, `grep`, `find_files`, `list_dir`, `pkg_info`,
`py_api`, `read_installed`, `grep_installed`, `gpu_info`) — but **no write tools** — so the Developer
reads the real eval/entry script and
the files it will change *before* deciding what to do, instead of planning blind off a truncated
preview. Two tools make this practical:

- **`read_file` paginates.** It takes `start_line` + `lines` to window a large file (like an editor's
  "go to line N, show M lines"); each reply is one page of at most ~3,600 characters of content that —
  when more of the file remains — ends with a `… (more below — continue with start_line=N)` resume
  marker, so the planner reads a file *once*, page by page from exactly where it left off, and a long
  file is never silently truncated mid-read.
- **`gpu_info`** reports the visible GPUs (count / names / memory via `torch.cuda`) — the `nvidia-smi`
  equivalent for an agent that has no shell, so the plan can size a model/batch to the real hardware.

## Phase-handoff summaries

Each LLM phase in a node build re-explores the same repo — the stages phase maps it, then plan reads
it again, then implement reads it again. **`phase_handoff_summary`** (on by default) cuts that with
**handoff briefs**, one `handoff_scope` the engine opens around each build: when an *exploration*
phase emits, ONE extra LLM call distills its whole transcript — the repo structure it mapped, the
files/data/APIs it confirmed, the decisions it made — into a tight brief injected into the **next**
phase's prompt, which is told to *trust it and not re-read*. The brief flows across the whole build
and **across the role boundary**: `Researcher·propose → Developer·stages → plan → implement`. Only
the exploration phases contribute (the ledger stays ≤3 briefs — no K-step bloat); terminal phases (a
single-session implement, each implement step, a repair) **consume** the briefs but don't summarize,
so there's no wasted call on the tail. The summary is best-effort (any error → the next phase just
runs without it), skipped for a phase that barely read anything, and produced by the Researcher's
propose phase only when the in-house repo Developer follows it (a single-shot developer never reads
the ledger, so the call would be wasted).

There is deliberately **no read cache**: every read tool call executes and returns fresh content
(a result that exceeds the ~4000-char tool-result cap is truncated with an explicit
`…[truncated by the tool-result cap …]` marker so the agent knows to re-request a narrower range);
the `StuckDetector` remains the safety net against true repeat loops. A parallel node build gets its own
scope; every phase runs through the shared `run_phase` wrapper, so with the setting off it's
byte-identical to a plain `drive_tool_loop`.

## Agentic auxiliary steps

Every remaining single-shot LLM step is now a **tool-using agent** (via the shared `agentic_text` /
`agentic_struct` helpers). Lessons distillation, the research + reward-hack / leakage **verify** pass,
the end-of-run **report**, and **Genesis** (goal → task plan) each *read* the real experiments / code
/ data before emitting. The **Strategist** likewise defaults to the agentic backend
(`strategist_backend=agent`). Novelty/dedup is decided the same way — the embedding / param search
only *suggests* near-duplicates and the LLM adjudicates; `novelty_gate` (and semantic novelty) stay
**off by default**.

## LLM-outage resilience

The LLM client is hardened against a flaky or throttling endpoint. A rate-limit-shaped **403** (a
proxy/WAF burst-throttle, not a real auth failure) is treated as retryable and backed off, and the
client makes up to **8 retries** (429 / 5xx / throttle-403) before surfacing an error. If the model
is genuinely unreachable, a Developer session crashes (`developer_crash`); the engine then **pauses
the whole run** on the *first* such crash (an `EV_PAUSE`) rather than rapid-firing dozens of dead
nodes — resume once the endpoint is back. Use `looplab timings RUN_DIR` to see where a run's
wall-clock actually went (LLM vs eval vs repair vs tools, per node).

## Signal delivery (agent synergy)

The engine computes rich, expensive signals — and each is only useful if it reaches the agent (or
human) that can act on it. The recurring failure mode is *"the signal is folded into the event log
but nothing injects it into a prompt"* — the same class the hint registry
(`roles.RESEARCHER_HINT_ATTRS`) already turned into a test-enforced invariant. LoopLab now routes
**seven** such signals, each through exactly one documented injection site:

| Signal | Folded into | Reaches | How |
|---|---|---|---|
| **Trust flags** (reward-hack / leakage) | `RunState.reward_hacks` | Researcher | a trust-reflection line in the proposal hint (`digest.trust_reflection`) — "a recent solution was flagged for X; avoid it if unintended" |
| **Crash-triage verdict** | `Node.triage_rationale` | Researcher | the failure line in the experiments digest + the failure-reflection hint carry the LLM's *why*, not just the error kind |
| **Foresight calibration** | `RunState.foresight_selected` | the world model | a track-record line in `_memory_brief` — "of your last N predict-before-execute picks, K beat the parent" (closes the predict→outcome loop) |
| **Deep-research memo** | `RunState.research` | Researcher | a one-line takeaway in the state brief **plus** a `read_research_memo` tool to pull the full findings/claims on demand |
| **Operator yields** | derived from the DAG | Strategist | a per-operator gain-per-second line in the strategist brief, so it tunes the operator mix from evidence, not priors |
| **Operator directives** | `RunState.pending_hints` | Researcher, Strategist, pilot, crash-triage, **Developer** | one `render_hint_directives` helper — the engine also folds directives into the idea handed to `implement`, so a directive steers the **code**, not only the proposal |
| **Run states** (paused / awaiting-approval / trust-flag / stuck-build) | `RunState` | boss / assistant | an "ATTENTION" block in the boss context, surfacing the states where human intervention is most valuable |

**The invariant.** `engine/signal_delivery.py` is a registry of these routes (signal → folded field
→ injection site → consumer), and `tests/test_signal_delivery.py` asserts each injection symbol
resolves *and* that a synthetic input's content actually reaches the rendered output. A signal added
to the registry without a delivery probe fails the suite — so *"the signal silently stopped being
delivered"* is a red test, not the next review's finding. Two of the routes (trust flags, operator
directives) are **push** (the engine injects them), one (deep-research memo) is **pull** (a tool the
agent may call for depth), and the rest ride the always-on folded-state briefs. The full rationale is
in `docs/14-agent-framework-mega-review-2026-07-10.md` §1.

## Knowledge, skills & prompts

Give the agentic Researcher extra context and tools:

| Setting | What it adds |
|---|---|
| `knowledge_dir` | A notes directory; the Researcher gets `grep` / `kb_search` / `list_notes` / `read_note` tools and chooses when to use them |
| `skills_dir` | A directory of `SKILL.md` files the Researcher can list and load |
| `prompt_dir` | Editable, hot-reloaded role-prompt `.md` files (override the built-in prompts) |
| `researcher_tools` | (on) Read its own experiments + the task data mid-loop |
| `cross_run_tools` | (on) Read-only tools over sibling runs (same task id, same run-root) |
| `all_runs_tools` | (on) Read-only tools over EVERY run on the machine, across ALL tasks — read any experiment's code + result to reuse it |
| `literature_search` | An arXiv search tool (network-optional) |
| `web_search` | Web search/fetch for the Deep-Research stage (network-optional) |

### Prompt override keys (`prompt_dir`)

Every built-in system prompt below can be replaced by dropping a `<key>.md` file into `prompt_dir`.
Files are **hot-reloaded** — re-read on every use, so you can tune a prompt mid-run without a
restart — and rendered with `string.Template` **`$var`** substitution (leading YAML frontmatter is
stripped). A missing file falls back to the built-in default.

| Key | Who uses it |
|---|---|
| `researcher_system` | The plain (non-tool) LLM Researcher — the CORE persona only: the sweep/`eval_timeout` capability suffix and the operator note are appended by code after the render (gated on the active backend's capabilities), so an override supplies just the core body |
| `developer_system` | The LLM Developer (both `implement` and `repair`) — the from-scratch script Developer |
| `developer_repair_prefix` | Short prefix prepended to `developer_system` on repair calls |
| `repo_developer_system_intro` | The in-house repo-editing Developer (`LLMRepoDeveloper`): the intro of its system prompt |
| `repo_developer_system_body` | The in-house repo-editing Developer (`LLMRepoDeveloper`): the body of its system prompt |
| `tool_researcher_system` | The tool-using Researcher — the default agentic Researcher |
| `strategist_system` | The plain LLM Strategist (meta-control decisions) |
| `tool_strategist_system` | The agent (tool-using) Strategist |
| `pilot_system` | The unified agent's action pilot (chooses the next macro action) |
| `triage_system` | The unified agent's crash triage (retry / repair / abandon) |
| `foresight_system` | The foresight ranker (predict-before-execute idea/hypothesis prioritization) |
| `bestofn_judge_system` | The best-of-N judge (picks the best of N candidate implementations) |
| `merge_system` | The hybrid-merge adjudicator (lesson & hypothesis-board consolidation); `$kind` and `$detail` vars |
| `deep_research_system` | The Deep-Research stage agent |

Migration note: a `researcher_system.md` override copied from the old full default should drop the
trailing sweep/`eval_timeout` capability text and the operator note — the code now appends those
fragments itself, so keeping them in the override would duplicate the prose.

```bash
looplab run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
```

With `--knowledge-dir`, the Researcher becomes a tool-using agent: in a bounded multi-turn loop it
may call the knowledge tools, then `emit` its structured idea. The orchestrator is unchanged — the
tool-using Researcher drops in behind the same protocol.

See [Configuration](configuration.md) for every related setting and [Concepts](concepts.md) for how
the roles fit into the loop.
