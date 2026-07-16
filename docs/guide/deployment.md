# Deployment

The local CLI needs **no Docker and no network**. This guide covers the two scenarios where extra
infrastructure matters: the **untrusted sandbox tier** and the **one-command Compose stack** for a
hosted setup.

## The untrusted sandbox tier

The sandbox tier is chosen by **trust mode**, not your environment:

| `trust_mode` | Sandbox | When |
|---|---|---|
| `trusted_local` (default) | `SubprocessSandbox` | Your own research on your own box — process isolation, timeout, tree-kill, output caps. No Docker. |
| `untrusted` | `DockerSandbox` (`--network none`) | Executing code on infrastructure that must protect other users (hosted / multi-tenant UI) |
| `hostile` | `DockerSandbox` (`--network none` + gVisor `--runtime runsc`) | Actively hostile code — kernel-level isolation on top of the untrusted tier (`make_sandbox` sets `runtime=runsc`) |

```bash
export LOOPLAB_TRUST_MODE=untrusted
export LOOPLAB_DOCKER_IMAGE=python:3.12-slim     # bake the framework's deps into this image
```

Because the container runs `--network none`, a candidate can't fetch anything at eval time — the
image must already contain the dependencies. Pair this tier with `redact_output=true` so a leaked
secret in a print/traceback never lands in the durable log.

> **Docker is only required for this tier (and the Compose stack below).** The local CLI never needs
> it.

## Docker Compose stack (LLM + UI + engine)

For the hosted scenario, `docker-compose.yml` brings up everything with one command. Requires Docker
with the NVIDIA GPU runtime (Docker Desktop + WSL2 is fine).

### Services

| Service | Role | Port |
|---|---|---|
| `sglang` | Serves a 4-bit MoE on the GPU via SGLang, OpenAI-compatible | `:30000` |
| `ui` | The live React UI + control plane (`looplab ui`) | `:8765` |
| `run` | A one-shot engine runner (compose profile `tasks`), started on demand | — |

### Bring it up

```bash
cp .env.example .env                       # model id, ports, context length, etc.
docker compose up -d sglang ui             # start the model + UI (first run downloads weights)
docker compose logs -f sglang              # watch the one-time model load (minutes)
# open http://localhost:8765

# run an autonomous experiment against the containerized model:
docker compose run --rm run \
    looplab run examples/regression_task.json --backend llm --max-nodes 14
```

LoopLab is wired to the model purely by env:

```bash
LOOPLAB_BACKEND=llm
LOOPLAB_LLM_BASE_URL=http://sglang:30000/v1
LOOPLAB_LLM_MODEL=...
```

The model, ports, VRAM fraction, context length, and SGLang flags are all tunable in `.env`. Run
artifacts land in `./runs`, shared with the host and the UI.

### Notes

- Structured output uses Qwen's native tool-call parser (`--tool-call-parser qwen`).
  `LOOPLAB_LLM_GUIDED_JSON` is **off** by default (SGLang's guided_json produced empty `{}` for some
  models) — set it to `1` in `.env` only if a weaker model needs constrained decoding.
- **Exposure:** both ports publish to `127.0.0.1` only by default. The UI control plane is
  unauthenticated unless `LOOPLAB_UI_TOKEN` is set, so it is not put on the LAN implicitly. To serve
  it beyond localhost, set a strong token, `UI_BIND=0.0.0.0`, and list every public hostname in
  `LOOPLAB_UI_HOSTS` (comma-separated). Host validation prevents DNS rebinding a browser into the
  local control plane. The token is not embedded in HTML: the owner enters it in the unlock screen
  and it stays in that browser tab's `sessionStorage`.

## Owner access and read-only review links

`LOOPLAB_UI_TOKEN` protects mutations and sensitive owner reads. When it is configured, the browser
first shows **Unlock LoopLab controls**; enter the same value there. The SPA sends it as
`X-LoopLab-Token` for the rest of that tab. Neither the owner page nor `/review` contains the token in
HTML, and it is not written to a persistent browser store.

The live owner run stream is protected by the same deny-by-default owner API boundary. The SPA uses an
authenticated `fetch`-based SSE client because native `EventSource` cannot attach `X-LoopLab-Token`; reconnects
send the last complete event ID in `Last-Event-ID`. Do not exempt `/api/runs/{id}/events` at a reverse proxy.
Read-only review links do not receive that owner stream: their dedicated `/api/review/*` projections stay
capability-scoped and polling-based. SSE responses are never gzip-buffered.

Inside an owner run, **Copy view** produces a generation-fenced diagnostic fragment such as
`#/run/<id>?gen=…&node=…&tab=…`. It copies context, not authority: a recipient still needs the owner
credential to open that URL. The generation remains in an otherwise-default copied view. Historical
Inspector/Report detail sends `seq` plus `expected_generation`; the server checks that generation
before and after folding without holding the command lock across the fold, and returns `409` if the
run was reset or replaced.

Read-only sharing is available only when this owner credential is configured. From a run's
**Lab → Collaboration** panel, the owner chooses an expiry (one hour through 30 days), optionally
includes redacted source evidence, and creates a link. The link is a one-run bearer capability:

- a review URL has the form `/review#/rv_…?gen=…&node=…`: the bearer and diagnostic state are both
  after `#`, never in the HTTP path/query or a proxy access log;
- the server stores only its digest under `<run-root>/.reviews/` and returns the bearer once;
- reviewer requests are confined to the dedicated GET-only `/api/review/*` namespace; an unknown API
  path returns JSON `404` rather than falling through to the owner SPA;
- every capability is bound to the exact 64-hex run generation captured while the run sequencer is
  held. Each review projection uses short pre/post sequencer fences for path/generation validation,
  while its state fold or metrics read runs outside the exclusive owner-command lock;
- `summary` includes the DAG/report, masked configuration, cost, and derived metrics;
- optional `evidence` adds redacted node source/results and parent diff through an explicit field
  allow-list, so a future Node field is not disclosed by default;
- minted review context is capability-scoped: summary permits summary-safe tabs/panels, evidence may
  add Code and Compare, and both remove historical `seq` plus raw Timeline `q`/`kinds`;
- raw logs and captured process output including `stdout_tail`, prompts, traces, live sidecars,
  artifacts, Assistant, owner settings, and every mutation remain unavailable;
- expiry, revocation, and generation binding are checked on every request. Reset, replacement,
  deletion, or a legacy/malformed missing generation ends the old link with `410 Gone` and **Review
  access ended**; the owner must create a new link for the replacement generation, and the old bearer
  is never retargeted. The owner list marks that old link `stale` and keeps **Revoke** available for
  explicit cleanup. Revocation blocks future reads but cannot erase material the recipient already
  copied.

Copying an exact review view preserves the existing bearer and only state allowed by that capability;
it does not add history, raw Timeline filters, or new evidence scope.

This is scoped read-only access, not an identity provider, RBAC system, or DLP guarantee. Do not share
from anonymous mode: the server refuses link creation without `LOOPLAB_UI_TOKEN`. Known credential
patterns are redacted from the optional evidence projection, but source can still contain sensitive
project information.

## Run as a JupyterHub app (jupyter-server-proxy)

LoopLab can launch as a **first-class app inside a JupyterHub single-user server** — a tile in the
Launcher that opens the live UI with no terminal and no hand-typed URL. Anonymous local mode can open
in-frame. When `LOOPLAB_UI_TOKEN` protects the owner shell, the tile opens a new browser tab because
the shell intentionally denies framing; the launcher never weakens that clickjacking boundary.
Install the extra:

```bash
pip install "looplab[jupyterhub]"      # fastapi + uvicorn + jupyter-server-proxy + psutil
```

The `jupyter_serverproxy_servers` entry point (`looplab/runtime/jupyter.py`) registers the tile: clicking it
runs `looplab ui` on a free port and proxies it at `/user/<name>/proxy/<port>/`. It selects the new-tab
target automatically when `LOOPLAB_UI_TOKEN` is present. Five env knobs matter on a hub:

| Env | Why |
|---|---|
| `LOOPLAB_RUN_ROOT` | Where runs persist. Defaults to `~/looplab-runs` (the user's home volume) so runs survive a hub idle-cull + pod restart instead of landing in an ephemeral CWD. **Don't** point it at an S3/geesefs FUSE mount — the append-only event log needs atomic rename. |
| `LOOPLAB_ALLOW_UNLOCKED_WRITER` | Safety override. The engine holds an exclusive `engine.lock` so only one writer touches a run's `events.jsonl`. On a FUSE/S3 mount where OS file locking is unavailable that lock can't be enforced, so startup **fails closed** with an actionable error (two writers could corrupt the log). Set this to `1` **only** if you guarantee a single engine per run dir; it degrades to a best-effort no-op and runs anyway. Prefer a lock-capable local disk (`LOOPLAB_RUN_ROOT`). |
| `LOOPLAB_UI_DIST` | A prebuilt React bundle. Set it (the image bakes one) so `looplab ui --no-build` serves instantly and never attempts an `npm build` on the noexec/FUSE home. |
| `LOOPLAB_UI_HOSTS` | Public hostname(s), comma-separated, that may reach the UI (for example `hub.example.org`). `localhost`, `127.0.0.1`, and `::1` are always allowed; every other Host is rejected to prevent DNS rebinding. |
| `LOOPLAB_LLM_BASE_URL` | The cluster LLM endpoint (the default is localhost Ollama). A wrong/unreachable endpoint now surfaces as a terminal `run_finished{reason:error}` event rather than a silent stuck run. |

**Behind a non-stripping proxy** `looplab ui` auto-derives `root_path` from `JUPYTERHUB_SERVICE_PREFIX`
(no raw-uvicorn fallback needed); a stripping proxy (jsp's default) is unaffected.

**Single-user image.** `Dockerfile.jupyterhub` builds a `quay.io/jupyter/base-notebook` image with
LoopLab installed, the bundle baked + pinned, and `LOOPLAB_RUN_ROOT` set. Point your Z2JH
`singleuser.image` (or `c.Spawner.image`) at it and every user gets a working LoopLab tile.

**Resource lifecycle.** Under JupyterHub the UI server reaps the engines it spawned on shutdown (a
hub cull would otherwise orphan a detached engine that keeps billing GPU/CPU and holds the run's
lock); eval subprocesses cap their BLAS/OpenMP threads to the pod's CPU quota; and an OOM-killed eval
is recognised and repaired (reduce batch/model size) instead of dying silently. These are no-ops on a
local box.

### Shared JupyterHub origin (important)

`LOOPLAB_UI_TOKEN` is a **per-deployment owner credential**, not per-user identity. It does not turn a
shared origin into an RBAC boundary.

Behind `jupyter-server-proxy`, every user's app lives under one origin —
`https://hub.example.org/user/alice/proxy/8765/`, `…/user/bob/proxy/8765/`, files at
`…/user/alice/files/…`, and other proxied apps can all live on `hub.example.org`. The same-origin
policy is **per-origin, not per-path**. LoopLab no longer embeds the owner token in the page, which
removes the former index-scraping path, and serves owner/review shells unframeable and `no-store`.
That is defence in depth, not isolation between mutually untrusted same-origin applications: they
still share the browser security principal, and the static token has no user identity or role.

LoopLab detects the hub (`JUPYTERHUB_SERVICE_PREFIX`) and logs this limitation at startup. A review
link remains server-enforced read-only, but it must not be presented as a substitute for upstream
authentication, reviewer identity, or tenant separation.

**For real per-user isolation, give each user a private origin** — a per-user subdomain
(`alice.hub.example.org`), a dedicated host/port reachable only by that user, or an authenticated
reverse proxy/network boundary — rather than a shared `…/proxy/<port>/` path. Treat
`LOOPLAB_UI_TOKEN` as the deployment owner's static control credential, not a wall between co-tenants.

## Observability export

Spans are always written to `spans.jsonl` (files-as-truth, zero-dep). To forward the *same* spans to
an OTLP collector (Jaeger / Tempo / Honeycomb), install the extra and set the standard env:

```bash
pip install -e ".[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
```

No code change is needed — the exporter bridges automatically when the packages and `OTEL_*` env are
present.
