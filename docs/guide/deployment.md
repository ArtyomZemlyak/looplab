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
  it beyond localhost, set a token and `UI_BIND=0.0.0.0` in `.env`.

## Run as a JupyterHub app (jupyter-server-proxy)

LoopLab can launch as a **first-class app inside a JupyterHub single-user server** — a tile in the
Launcher that opens the live UI in-frame, no terminal and no hand-typed URL. Install the extra:

```bash
pip install "looplab[jupyterhub]"      # fastapi + uvicorn + jupyter-server-proxy + psutil
```

The `jupyter_serverproxy_servers` entry point (`looplab/runtime/jupyter.py`) registers the tile: clicking it
runs `looplab ui` on a free port and proxies it at `/user/<name>/proxy/<port>/`. Three env knobs
matter on a hub:

| Env | Why |
|---|---|
| `LOOPLAB_RUN_ROOT` | Where runs persist. Defaults to `~/looplab-runs` (the user's home volume) so runs survive a hub idle-cull + pod restart instead of landing in an ephemeral CWD. **Don't** point it at an S3/geesefs FUSE mount — the append-only event log needs atomic rename. |
| `LOOPLAB_UI_DIST` | A prebuilt React bundle. Set it (the image bakes one) so `looplab ui --no-build` serves instantly and never attempts an `npm build` on the noexec/FUSE home. |
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

`LOOPLAB_UI_TOKEN` isolates users **only when each principal has its own origin** — the default
`127.0.0.1` bind, or a per-user subdomain. It does **not** make the token per-user on a shared
origin.

Behind `jupyter-server-proxy`, every user's app lives under one origin —
`https://hub.example.org/user/alice/proxy/8765/`, `…/user/bob/proxy/8765/`, files at
`…/user/alice/files/…`, other proxied apps — all on `hub.example.org`. The same-origin policy is
**per-origin, not per-path**, so any same-origin page running in the user's browser can `fetch()`
the app's index, regex out the injected `ll-token`, and replay it to drive the control plane
(start/delete runs, edit configs, run shell-executing experiments). On a shared hub the token is a
**per-deployment secret, not a per-user one.**

LoopLab detects the hub (`JUPYTERHUB_SERVICE_PREFIX`) and logs a warning at startup. As
defence-in-depth it injects the token only on a genuine top-level navigation (`Sec-Fetch-Dest:
document`) — never on a programmatic `fetch`/XHR or a framed load — and serves the token-bearing
page with `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, and
`Cache-Control: no-store`. That blocks the common `fetch('/').then(r => r.text())` scrape and the
iframe-reads-`contentDocument` trick, **but it is not a complete fix**: a same-origin page can still
`window.open()` the app and read the popup.

**For real per-user isolation, give each user a private origin** — a per-user subdomain
(`alice.hub.example.org`), a dedicated host/port reachable only by that user, or network isolation —
rather than a shared `…/proxy/<port>/` path. Treat `LOOPLAB_UI_TOKEN` on a shared hub as a
deployment-wide gate (keeps *other* hubs/origins out), not as a wall between co-tenant users.

## Observability export

Spans are always written to `spans.jsonl` (files-as-truth, zero-dep). To forward the *same* spans to
an OTLP collector (Jaeger / Tempo / Honeycomb), install the extra and set the standard env:

```bash
pip install -e ".[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
```

No code change is needed — the exporter bridges automatically when the packages and `OTEL_*` env are
present.
