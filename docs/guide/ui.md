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

On the first launch `looplab ui` **builds the React bundle automatically** when it's missing and
Node/npm are on your `PATH` (`npm ci` + `npm run build`, once) — so a fresh `pip install` needs no
manual build step. If Node isn't installed it prints how to build by hand and still starts the API.

| Option | Default | Description |
|---|---|---|
| `--run-root DIR` | `runs` | Directory containing run subdirectories |
| `--host HOST` | `127.0.0.1` | Bind host |
| `--port PORT` | `8765` | Bind port |
| `--build / --no-build` | `--build` | Auto-build the bundle if missing (`--no-build` to skip) |
| `--rebuild` | off | Force a fresh `npm run build` even if a bundle already exists |

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
the built bundle, not the source. Easiest is to let the CLI do it:

```bash
looplab build-ui --force   # npm ci (first time) + npm run build into ui/dist
# or run vite directly:
cd ui && npm install && npm run build
```

`looplab ui --rebuild` does the same and then serves. For live HMR while hacking on the UI, run the
Vite dev server (`cd ui && npm run dev`) against the API.

A preview launcher (`ui_preview.py`) serves the built UI with the dev `.env.dev` on a dedicated port
(`:8771`) so a review session can run alongside the main instance.

## Troubleshooting

**`EACCES` executing a file under `node_modules` (e.g. esbuild), or `vite: not found`.** Vite's
`esbuild` runs a **native binary** during install/build. `EACCES` when *executing* it means the
volume holding `node_modules` won't run binaries. Two common causes:

- a **`noexec`** mount (NFS / mounted data volumes), or
- an **object-store FUSE mount** — `fuse.geesefs`, `s3fs`, `goofys` (common on JupyterHub `~/data`).
  S3-backed filesystems don't preserve the Unix executable bit and lack atomic renames/hardlinks, so
  the install can't run the binary *and* often aborts half-way — which then shows up as
  `vite: not found` (the `.bin` shims were never created). `chmod +x` can't fix either case.

Confirm the mount, then build on the pod's **local** disk and copy only the built static bundle back
(serving `dist/` is read-only, so an S3 mount handles it fine):

```bash
findmnt -T . -o TARGET,FSTYPE,OPTIONS        # fuse.geesefs / s3fs / a `noexec` option => build elsewhere

# build on local exec disk (/tmp), then copy the bundle to the repo's DEFAULT ui/dist
rm -rf /tmp/ll-ui && cp -r ./ui /tmp/ll-ui && rm -rf /tmp/ll-ui/node_modules
cd /tmp/ll-ui && npm ci && npm run build
rm -rf "$OLDPWD/ui/dist" && cp -r dist "$OLDPWD/ui/dist"   # back to the default path
cd "$OLDPWD" && looplab ui                                  # finds ui/dist, no rebuild, no env var
```

Putting the bundle at the default `ui/dist` means no env var and it persists across pod restarts.
Alternatively keep it on local disk and pin `export LOOPLAB_UI_DIST=/tmp/ll-ui/dist` (= "use this
prebuilt bundle, never rebuild" — also how the Docker image ships its bundle).

For the containerized UI + model + engine, see [Deployment](deployment.md).
