# Web UI

LoopLab ships a live React control plane. It's a **separate read/control process** — it tails each
run's `events.jsonl`, folds it with `replay.fold`, streams the state to the browser over SSE, serves
the built React app, and turns UI actions into appended control events. It never changes the engine
and is never imported by it (ADR-18).

> **No browser? Use the terminal.** `looplab tui` is a chat-first **terminal control plane** over the
> same server — a run dashboard, the "describe a goal → the boss launches it" genesis flow, and a
> per-run boss chat that steers the live run. It auto-launches an API-only server when none is found,
> so `looplab tui` works on its own. See the [CLI reference](cli-reference.md#tui). It's the *control*
> slice; come back to this web UI to explore the search DAG, traces and per-node detail.

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
- **Create a run by describing it** — the main-menu chat ("New run") turns a plain-text goal into an
  editable run spec: the boss invents a name, picks or authors the task, and sets the knobs (model,
  node budget, seeds, policy). It also authors **repo runs** — point it at a repo to optimize and it
  fills the repo path, the run/eval command, the metric key, and the edit surface, plus an
  **adaptation checklist** (how to make the repo LoopLab-ready: expose a JSON metric, pin deps, choose
  the edit surface, protect the grader). For a repo it's a real **agent**: it first *reads your repo
  on disk* (README, the eval/entry script, requirements, results files) through read-only scout tools
  so the command, metric, and steps are grounded in your actual code — not guessed. When the authored
  task needs a code-writing agent (repo / dataset / Kaggle — the generative kinds), the card also
  defaults `backend=llm`, matching `looplab run --goal`, so a UI-launched run never silently falls
  back to the offline toy developer; a backend you set explicitly (in the card, the Settings page, or
  `LOOPLAB_BACKEND`) always wins. Tweak any field,
  then launch. See **[Generating train & test code](generating-code.md)** for the full Genesis flow
  and every "let the agent write the code" case (from-scratch, repo edit, test-without-train,
  onboarding) plus how to point at your data.
- **Drive a run** — start, resume, fork, branch, or inject nodes from the browser; the server spawns
  the engine as a subprocess. A finished run can be extended with a new batch.
- **Reset a node in place** — the node inspector's **↻ Reset** button (or `reset(node_id, stage)` in
  chat) re-runs an EXISTING node from a chosen stage instead of spawning a new one: `eval` re-scores it
  (keep the idea + code — for an infra/API-key blip), `implement` re-runs only the Developer (keep the
  Researcher's idea — for crashed code), `propose` is a full redo. Any eval-**pipeline** stage name
  (`train`, `data_prep`, …) is also accepted — it restarts the node's pipeline from that stage,
  reusing earlier stages' artifacts. Same node id, no proliferation;
  applied on the next resume (`node_reset` control event).
- **Chat / boss** — an agentic run chat turns one message into a plan of ordered actions, with each
  action narrated in a durable feed (`chat.jsonl`).
- **Reports** — an agent-authored, conclusion-first run report plus deterministic metric-improvement
  charts.
- **Trust panel** — surfaces the safety monitors (reward-hack, code-leakage, critic flags); set
  `trust_gate` to `gate`/`block` (or pick the `thorough` profile) to make a flagged node ineligible
  to win, not just logged.
- **Hypotheses board** — a kanban of what the run is trying to learn (open / testing / supported /
  tested / abandoned). Each experiment states the hypothesis it tests; deep-research directions and
  your own "+ Add" questions land here too, then get tracked to a verdict with links to the evidence
  nodes. Audit-only — it never changes which node wins.
- **Per-node trace** — when `trace_llm_io` is on, see exactly what the model read and wrote per node.
- **Per-run settings** — edit a run's settings; `PUT /api/runs/{id}/config` rewrites that run's
  snapshot (resume reads it, not the global UI defaults).
- **Settings page** — every engine knob, grouped into tabs (Search, Strategist & operators,
  Resilience, Budgets, LLM, Developer agent, Safety & trust, Knowledge & memory). The **API key**
  field (LLM tab) stores the credential securely: it's written to an owner-only `secrets.json`, never
  to `ui_settings.json` or a run snapshot, and the API only ever echoes a masked `***`. Set it here or
  via `LOOPLAB_LLM_API_KEY` (env / `.env`) — either way spawned runs inherit it.

## Exposure & auth

Bind to `127.0.0.1` (the default) for local use. The control plane is **unauthenticated** unless you
set a token, so it is not placed on the LAN implicitly. To serve beyond localhost, set
`LOOPLAB_UI_TOKEN` and bind to `0.0.0.0`.

`LOOPLAB_UI_TOKEN` separates users **only when each has its own origin** (the `127.0.0.1` bind, or a
per-user subdomain). On a **shared** origin — notably a JupyterHub `…/user/<name>/proxy/<port>/` path
where every user shares one origin — the same-origin policy is per-origin, not per-path, so the token
is a per-deployment secret rather than a per-user one. LoopLab warns at startup and hardens the
token-bearing page (token injected only on a top-level navigation; `X-Frame-Options`/`no-store`), but
the real fix is a **private origin per user**. See deployment.md → *Shared JupyterHub origin*.

### Behind a path-mounting proxy (JupyterHub, reverse-proxy subpath)

The UI works when it's served under a path prefix — e.g. JupyterHub's
`/user/<name>/proxy/8765/` (`jupyter-server-proxy`). The build references its assets relatively and
joins the served prefix on every API/SSE call, so no extra config is needed for the common
prefix-**stripping** proxy (`/proxy/<port>/`): keep the default `--host 127.0.0.1` (the proxy reaches
it on localhost) and open the proxy URL. If your proxy does **not** strip the prefix before
forwarding, start uvicorn with a matching `root_path` (set `--host`/port as usual and run behind
`uvicorn ... --root-path /user/<name>/proxy/8765`).

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

A preview launcher (`tools/ui_preview.py`) serves the built UI with the dev `.env.dev` on a dedicated port
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
