# Web UI

LoopLab ships a live React control plane. It's a **separate read/control process** — it tails each
run's `events.jsonl`, folds it with `replay.fold`, streams the state to the browser over SSE, serves
the built React app, and turns UI actions into appended control events. It never changes the engine
and is never imported by it (ADR-18).

## Install & launch

The UI needs the `[ui]` extra:

```bash
pip install -e ".[ui]"
looplab ui                      # serves http://127.0.0.1:8765 over ./runs
```

| Option | Default | Description |
|---|---|---|
| `--run-root DIR` | `runs` | Directory containing run subdirectories |
| `--host HOST` | `127.0.0.1` | Bind host |
| `--port PORT` | `8765` | Bind port |

```bash
looplab ui --run-root runs --host 127.0.0.1 --port 8765
```

Then open the printed URL. The server serves the **built** React bundle from `ui/dist/`.

## What it does

- **Live runs** — watch a run unfold in real time over SSE: the lineage graph, per-node metrics,
  status, and tokens. Reopening a finished run still streams (runs are self-describing via
  `task.snapshot.json`).
- **Drive a run** — start, resume, fork, branch, or inject nodes from the browser; the server spawns
  the engine as a subprocess. A finished run can be extended with a new batch.
- **Chat / boss** — an agentic run chat turns one message into a plan of ordered actions, with each
  action narrated in a durable feed (`chat.jsonl`).
- **Reports** — an agent-authored, conclusion-first run report plus deterministic metric-improvement
  charts.
- **Trust panel** — surfaces the audit-only safety monitors (reward-hack, code-leakage, critic
  flags).
- **Per-node trace** — when `trace_llm_io` is on, see exactly what the model read and wrote per node.
- **Per-run settings** — edit a run's settings; `PUT /api/runs/{id}/config` rewrites that run's
  snapshot (resume reads it, not the global UI defaults).

## Exposure & auth

Bind to `127.0.0.1` (the default) for local use. The control plane is **unauthenticated** unless you
set a token, so it is not placed on the LAN implicitly. To serve beyond localhost, set
`LOOPLAB_UI_TOKEN` and bind to `0.0.0.0`.

## Developing the UI

The frontend lives in `ui/` (Vite + React). Changes to the JSX require a rebuild — the server serves
the built bundle, not the source:

```bash
cd ui
npm install
npm run build          # rebuild ui/dist so `looplab ui` serves the new bundle
```

A preview launcher (`ui_preview.py`) serves the built UI with the dev `.env.dev` on a dedicated port
(`:8771`) so a review session can run alongside the main instance.

For the containerized UI + model + engine, see [Deployment](deployment.md).
