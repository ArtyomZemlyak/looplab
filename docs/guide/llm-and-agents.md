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

## The Developer: read-only planning + env inspection

Before it writes anything, the repo Developer runs a **read-only PLAN stage**: it gets the read-only
repo scouts plus the env inspector (`read_file`, `grep`, `find_files`, `list_dir`, `pkg_info`,
`py_api`, `gpu_info`) — but **no write tools** — and its only output is an ordered plan. So it reads
the real eval/entry script and the files it will change *before* deciding what to do, instead of
planning blind off a truncated preview. Two tools make this practical:

- **`read_file` paginates.** It takes `start_line` + `lines` to window a large file (like an editor's
  "go to line N, show M lines"), so the planner reads a file *once* and resumes from where it left off
  rather than only ever seeing the first 16 KB.
- **`gpu_info`** reports the visible GPUs (count / names / memory via `torch.cuda`) — the `nvidia-smi`
  equivalent for an agent that has no shell, so the plan can size a model/batch to the real hardware.

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

```bash
looplab run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
```

With `--knowledge-dir`, the Researcher becomes a tool-using agent: in a bounded multi-turn loop it
may call the knowledge tools, then `emit` its structured idea. The orchestrator is unchanged — the
tool-using Researcher drops in behind the same protocol.

See [Configuration](configuration.md) for every related setting and [Concepts](concepts.md) for how
the roles fit into the loop.
