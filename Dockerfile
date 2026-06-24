# syntax=docker/dockerfile:1
#
# LoopLab full-stack image: the React UI (built once with Node) served by the
# FastAPI/uvicorn control-plane, plus the engine CLI (`LoopLab run ...`).
# One image powers both the `ui` and `run` compose services.
#
# This image is ONLY for the Docker-Compose deploy path. Local single-user use
# stays zero-dependency (see README "Docker is not required").

# ---- Stage 1: build the React UI -> /ui/dist ---------------------------------
FROM node:20-slim AS ui-build
WORKDIR /ui
# Install deps first (cached unless the lockfile changes).
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build   # vite build -> /ui/dist

# ---- Stage 2: Python runtime (engine + UI server) ----------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

# git: needed by the agent patch-gate (git worktree) when an external coding
# agent Developer is used. curl: handy for healthchecks/debugging.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

# Install the package with the UI ([ui] -> fastapi+uvicorn) and process
# ([proc] -> psutil) extras. Copy only what's needed to build the wheel first
# so dependency install layers cache across source edits.
COPY pyproject.toml README.md ./
COPY looplab/ ./looplab/
RUN pip install --no-cache-dir ".[ui,proc]"

# Runtime assets the engine/UI read at run time.
COPY examples/ ./examples/
COPY tools/ ./tools/
# Built React app served same-origin by the FastAPI server.
COPY --from=ui-build /ui/dist ./ui/dist

# Where the server looks for built UI assets (overridable).
ENV LOOPLAB_UI_DIST=/app/ui/dist \
    PYTHONUNBUFFERED=1

# Run dirs live here; mounted as a volume by compose so the engine and UI share them.
RUN mkdir -p /app/runs
EXPOSE 8765

# Default: serve the live UI on all interfaces (compose maps it to the host).
CMD ["LoopLab", "ui", "--host", "0.0.0.0", "--port", "8765", "--run-root", "/app/runs"]
