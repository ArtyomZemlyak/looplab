# Deployment

The local CLI needs **no Docker and no network**. This guide covers the two scenarios where extra
infrastructure matters: the **untrusted sandbox tier** and the **one-command Compose stack** for a
hosted setup.

## The untrusted sandbox tier

The sandbox tier is chosen by **trust mode**, not your environment:

| `trust_mode` | Sandbox | When |
|---|---|---|
| `trusted_local` (default) | `SubprocessSandbox` | Your own research on your own box — process isolation, timeout, tree-kill, output caps. No Docker. |
| `untrusted` | `DockerSandbox` (`--network none` → gVisor) | Executing code on infrastructure that must protect other users (hosted / multi-tenant UI) |

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

## Observability export

Spans are always written to `spans.jsonl` (files-as-truth, zero-dep). To forward the *same* spans to
an OTLP collector (Jaeger / Tempo / Honeycomb), install the extra and set the standard env:

```bash
pip install -e ".[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
```

No code change is needed — the exporter bridges automatically when the packages and `OTEL_*` env are
present.
