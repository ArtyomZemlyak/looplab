# Installation

## Requirements

- **Python ≥ 3.11** on Linux, macOS or Windows. No Docker and no network are needed for local runs.
- **Node and npm** for a source checkout's first UI build: `ui/package.json` accepts Node `^20.19.0`,
  `^22.13.0` or `>=24.0.0`. `looplab ui` builds a missing or stale bundle when it starts.
- **A model** for real work: any OpenAI-compatible endpoint (Ollama, vLLM, SGLang, OpenAI). The
  offline demo needs none.

## Source install for the web UI

The recommended install: the engine, the CLI and the web UI. On Linux/macOS:

```bash
git clone https://github.com/ArtyomZemlyak/looplab.git
cd looplab
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[ui]"
.venv/bin/looplab ui
```

On Windows PowerShell:

```powershell
git clone https://github.com/ArtyomZemlyak/looplab.git
Set-Location looplab
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[ui]"
.\.venv\Scripts\looplab.exe ui
```

Open `http://127.0.0.1:8765`; runs are stored under `./runs`. Then follow the
[Quickstart](quickstart.md#assistant-in-the-web-ui). If `looplab` is not on `PATH`, the same CLI is
`python -m looplab.cli`.

## Check the install

```bash
looplab run examples/demo.yaml     # offline: six experiments in a few seconds
looplab inspect runs/demo          # the best result and why the run stopped
looplab smoke                      # once a model is configured: one request to check it
```

## Optional extras

Combine extras as needed, e.g. `pip install -e ".[ui,harness]"`.

| Extra | Adds | Without it |
|---|---|---|
| `ui` | `looplab ui` and local auto-start for `looplab tui` (FastAPI, uvicorn) | The CLI and the static `tree.html` still work |
| `harness` | `looplab harness-mcp` for Codex, Claude Code and other MCP clients | No external-agent control |
| `jupyterhub` | A JupyterHub launcher tile and proxied UI | Start `looplab ui` yourself |
| `otel` | Span export to an OTLP collector (Jaeger, Tempo, Honeycomb) | Spans still go to `spans.jsonl` |
| `proc` | Reliable process-tree termination (psutil) | Best-effort kill on timeout |
| `docs` | `mkdocs serve` for this site | — |
| `dev` | The test suite (pytest, pytest-split, ruff, MCP SDK) | — |

A plain `pip install -e .` installs only the engine and CLI. Its dependencies are small:
`pydantic`, `pydantic-settings`, `orjson`, `anyio`, `typer`, `PyYAML`, and `openai` + `httpx` for the
live model transport.

## Optional external tools

- **Docker** with the NVIDIA runtime, only for the `untrusted` sandbox tier or the Compose stack —
  [Deployment](deployment.md).
- **An external coding agent** (`opencode`, `aider`, `goose`, `continue`) to delegate the Developer
  role — [LLM & coding agents](llm-and-agents.md).
- **MLflow** (`pip install mlflow`) for `looplab export-mlflow` and the live mirror; without it both
  do nothing.
