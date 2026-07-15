# Web UI

LoopLab ships a live React control plane. It's a **separate read/control process** — it tails each
run's `events.jsonl`, folds it with `replay.fold`, streams the state to the browser over SSE, serves
the built React app, and submits interactive controls through the server-owned durable command
lifecycle. It never changes the engine in-process and is never imported by it (ADR-18).

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
  status, tokens, and returned provider-reported paid cost. Per-call numeric usage is append-only, so the total
  already written to `events.jsonl` survives resume/new engine processes. Same-ID ledger retries do
  not double-count. Before append, the ledger first attempts to atomically retain a numeric-only
  delta in the run-local `.llm-usage-outbox`; a successful outbox rename or event append is the first
  durable boundary, and a later reconciliation in a fresh Engine/server process can finish the same-ID append. Reset/delete
  drain it fail-closed and reset archives it with the old generation. This is observed
  run-attributable usage, not invoice reconciliation: a process kill before either first durable
  persistence completes or an ambiguous paid timeout/reset/empty-response retry can leave an unknown
  charge, and missing final usage cannot be invented. The overall command/activity service is
  validated for the supported single UI server process, not a multi-worker deployment. Genesis, `/api/research`, global
  Assistant, cross-run scope reports, health probes, and other non-run-scoped calls are excluded.
  Reopening a finished run still
  streams (runs are self-describing via `task.snapshot.json`).
- **Create a run by describing it** — the main-menu chat ("New run") turns a plain-text goal into a
  proposed run spec: the boss invents a name, picks or authors the task, and sets the knobs (model,
  node budget, seeds, policy). It also authors **repo runs** — point it at a repo to optimize and it
  fills the repo path, the run/eval command, the metric key, and the edit surface, plus an
  **adaptation checklist** (how to make the repo LoopLab-ready: expose a JSON metric, pin deps, choose
  the edit surface, protect the grader). For a repo it's a real **agent**: it first *reads your repo
  on disk* (README, the eval/entry script, requirements, results files) through read-only scout tools
  so the command, metric, and steps are grounded in your actual code — not guessed. When the task
  needs a code-writing agent (repo / dataset / Kaggle — the generative kinds), the **launch itself**
  defaults `backend=llm` — the rule lives in `/api/start`, the funnel every launch goes through
  (genesis cards, assistant-proposed runs, direct API calls), matching `looplab run --goal` — so a
  UI-launched run never silently falls back to the offline toy developer; the genesis card shows the
  inferred backend up front for review. The current card is not an inline editor: ask the Assistant
  for a revised proposal before starting if a field is wrong. A backend set explicitly in Settings or
  `LOOPLAB_BACKEND` still wins. See **[Generating train & test code](generating-code.md)** for the full
  Genesis flow
  and every "let the agent write the code" case (from-scratch, repo edit, test-without-train,
  onboarding) plus how to point at your data.
- **Drive a run** — start, resume, fork, branch, or inject nodes from the browser; the server spawns
  the engine as a subprocess. New-run start uses its dedicated launch route and the shared spawn
  lease. A finished run can be extended with a new batch. Existing-run interactive controls use one
  idempotent, observable [command lifecycle](concepts.md#authoritative-command-lifecycle),
  shared with the boss and TUI, so pending work is not presented as completed. `accepted` and
  `executing` remain pending; an engine acknowledgement means the engine observed that exact intent,
  not that all resulting domain work is done. While finalization or terminal write-out is active, the
  run list/header/Dock show it and hide conflicting Resume/Replay controls. If the Web response is
  lost or temporarily unreadable, Dock and Assistant preserve the same command identity and offer
  **Check same command** or, for an eligible terminal failure, **Retry same command** instead of
  silently submitting a fresh action. Both surfaces persist a sanitized allowlisted envelope and one
  exact per-run tab lock before POST; if session storage cannot be written, the command is not sent.
  Corrupt, tampered, mismatched, or unsafe stored state is quarantined and never replayed. The shared
  lock makes an Assistant command visible in Dock and blocks a competing same-run action on either
  surface, while commands for other runs remain independent. Assistant keeps failures across reload,
  attributes an in-flight result only to the run that originated it, and uses focused live status/error
  regions plus touch-sized wrapping recovery controls on narrow screens. Structured conflicts explain
  whether an identical command can be reattached or a different active command must finish first.
  Model-driven command-backed run mutations are staged in a durable per-turn journal before
  execution; recovering an unanswered turn may replay only its exact command-backed intents, while
  changed/new or uncertain direct-storage mutations are blocked. Recovery pins the persisted raw
  instruction and mode, rejects
  either mismatch, and exposes only read/Todo tools plus journal-backed run control — no file, shell,
  git, knowledge, MCP, proposal, or subagent mutators. A different message cannot overtake a dangling
  or still-cancelling turn. The TUI likewise stages the exact key and deep-copied payload before POST
  and uses same-key recovery when an early 404 races a delayed original request.
  On Web reload/session re-open, a dangling `turn_id` is re-read and recovered with its persisted
  `raw || content`, clean display, and exact mode; the UI polls that same turn without adding a second
  user bubble. A changed/corrupt identity is blocked instead of retried with rebuilt context. Retry of
  a completed persisted turn is a new turn, but it also reuses that durable raw/display/mode exactly.
  Reset preserves terminal command records and run-scoped background LLM/report work holds a
  generation lease. State/SSE supplies a stable generation token that Web, Assistant, and TUI persist
  with each fresh command before POST. If a request formed on generation A first arrives after Replay
  created B, the server returns `409 run_generation_changed` before any command record, event, or
  process side effect. Same-key recovery of an already-accepted A command remains observational.
  Natural finish reports use a durable planned/attempt/result boundary. A restart safely performs a
  report only when no attempt marker exists, reuses a scoped durable report, and records an ambiguous
  paid attempt as incomplete instead of issuing a second provider call.
  Standalone legacy CLI `stop`/`finalize`/`resume`/`approve` commands are still outside this server
  sequencer and should not be run concurrently with an active server-owned command.
- **Review Assistant actions before they run** — permission cards show the server-derived risk,
  exact scope, consequence, active mode, and request expiry. The newest card opens the Assistant and
  focuses **Reject**; resolve buttons remain locked until the server answers. A short remembered grant
  applies only to the same session, mode, current turn, action, and scope. It is never offered for
  high-risk or unclassified actions, and `Auto` still asks before arbitrary shell/test execution,
  destructive operations, external MCP calls, and unknown capabilities. Plan remains read-only and
  does not expose shared-memory writes. Direct mutation APIs and non-Assistant browser confirms are
  not yet all unified under this card contract; keep the UI on a private/authenticated control plane.
- **Reset a node in place** — the node inspector's **↻ Reset** button (or `reset(node_id, stage)` in
  chat) re-runs an EXISTING node from a chosen stage instead of spawning a new one: `eval` re-scores it
  (keep the idea + code — for an infra/API-key blip), `implement` re-runs only the Developer (keep the
  Researcher's idea — for crashed code), `propose` is a full redo. Any eval-**pipeline** stage name
  (`train`, `data_prep`, …) is also accepted — it restarts the node's pipeline from that stage,
  reusing earlier stages' artifacts. Same node id, no proliferation. The command service wakes or
  attaches the driver automatically. Its exact `command_ack` means the engine accepted that reset
  intent; re-development/re-evaluation may still be running and remains visible as normal run work.
- **Chat / boss** — an agentic run chat turns one message into a plan of ordered actions, with each
  action narrated in a durable feed (`chat.jsonl`).
- **Reports** — an agent-authored, conclusion-first run report plus deterministic metric-improvement
  charts.
- **Read-only review links** — with `LOOPLAB_UI_TOKEN` configured, **Lab → Collaboration** creates a
  revocable, expiring capability for one run. Summary links expose the DAG/report and derived metrics;
  an explicit evidence option adds redacted node source/results. Assistant, actions, raw
  logs/prompts/traces, artifacts, and owner settings are never available to the recipient.
- **Comment threads** — event-sourced operator discussion pinned to a run or a specific node, with an
  edit history and a resolve/reopen state. The view is served as authenticated current + history
  projections (`GET /api/runs/{run_id}/comments`, `…/comments/{id}/history`); the operator writes the
  `comment_created` / `comment_edited` / `comment_resolution_changed` control intents and the projection
  (`events/comment_projection.py`) derives the threaded state — the engine stays the sole writer of
  domain events (distinct from the legacy single `annotation` event).
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
`LOOPLAB_UI_TOKEN`, bind to `0.0.0.0`, and add the public hostname to the comma-separated
`LOOPLAB_UI_HOSTS` allow-list. Requests with any other Host are rejected, closing DNS-rebinding
attacks against the local API.

The token is never embedded in HTML. The owner enters it at **Unlock LoopLab controls** and it remains
in that tab's `sessionStorage`. True review links cannot be created in anonymous mode; the reviewer
uses a separate tokenless `/review` shell and a server-enforced GET-only capability.

`LOOPLAB_UI_TOKEN` is a static deployment-owner credential, not per-user identity or RBAC. On a
shared origin — notably a JupyterHub `…/user/<name>/proxy/<port>/` path — other applications still
share one browser security principal. Use a private origin or authenticated reverse proxy for hostile
multi-user isolation. See the [deployment guide](deployment.md#shared-jupyterhub-origin-important).

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
