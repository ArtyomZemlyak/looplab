# Installation

## Requirements

- **Python ≥ 3.11**
- A POSIX or Windows shell. The local engine needs **no Docker and no network** by default.

## Install

From a clone of the repository:

```bash
pip install -e .
```

This installs the engine and the `looplab` command. The core dependency set is intentionally small:

| Package | Why |
|---|---|
| `pydantic` / `pydantic-settings` | Typed domain models + layered settings |
| `orjson` | Fast JSON for the event log |
| `anyio` | The async control loop |
| `typer` | The CLI |
| `PyYAML` | YAML configuration input |
| `openai` / `httpx` | OpenAI-compatible live transport (import-safe for offline replay) |

`orjson` and transitive packages such as `pydantic-core` use prebuilt native wheels on common
platforms, so the dependency set should not be described as pure Python.

## Optional extras

Install only what you need:

```bash
pip install -e ".[ui]"      # live React web UI       → adds fastapi, uvicorn
pip install -e ".[otel]"    # OpenTelemetry export      → adds opentelemetry-*
pip install -e ".[proc]"    # robust process tree-kill  → adds psutil
pip install -e ".[jupyterhub]" # JupyterHub app tile      → adds UI + jupyter-server-proxy + psutil
pip install -e ".[dev]"     # test dependencies         → adds pytest, httpx, fastapi, uvicorn
```

You can combine them: `pip install -e ".[ui,otel,dev]"`.

| Extra | Unlocks | Without it |
|---|---|---|
| `ui` | `looplab ui` and local auto-start for `looplab tui` — the live control planes | Core CLI + static `tree.html` still work; TUI can target an existing server with `--server URL` |
| `otel` | Sends spans to any OTLP collector (Jaeger/Tempo/Honeycomb) | Spans still written to `spans.jsonl` (files-as-truth) |
| `proc` | Cross-platform process-tree termination on timeout | Falls back to best-effort kill |
| `jupyterhub` | JupyterHub launcher tile and proxied UI server | Run the CLI/UI directly instead |
| `dev` | Runs the test suite | — |

## Optional runtime components

These are **not Python extras** — they're external tools you point LoopLab at:

- **A live LLM endpoint** (Ollama / vLLM / SGLang / OpenAI) for `--backend llm`. See
  [LLM & coding agents](llm-and-agents.md).
- **An external coding agent** (`opencode` / `aider` / `goose` / `continue`) to delegate the
  Developer role. See [LLM & coding agents](llm-and-agents.md).
- **Docker** with the NVIDIA runtime, only for the `untrusted` sandbox tier or the Compose stack.
  See [Deployment](deployment.md).
- **MLflow** (`pip install mlflow`) only for `looplab export-mlflow`.

## Verify the install

```bash
python -c "import looplab; print(looplab.__version__)"   # 0.1.0
looplab --help                                            # CLI is on PATH
looplab run examples/toy_task.json --out runs/check --max-nodes 4
```

If `looplab` is not found after install, the same CLI is always reachable as
`python -m looplab.cli`.

Next: the [Quickstart](quickstart.md).
