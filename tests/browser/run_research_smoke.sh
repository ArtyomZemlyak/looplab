#!/usr/bin/env bash
# CPU-only, isolated local fixture. Install Playwright/Chromium separately; no live server or GPU is used.
set -euo pipefail
: "${LOOPLAB_PLAYWRIGHT_MODULE:?Set LOOPLAB_PLAYWRIGHT_MODULE to playwright/index.mjs}"
: "${LOOPLAB_PYTHON:=python}"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/looplab-browser-fixture.XXXXXX")
port=$("$LOOPLAB_PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
LOOPLAB_BROWSER_FIXTURE_ROOT="$fixture_root" PYTHONPATH=. "$LOOPLAB_PYTHON" -m uvicorn research_fixture:app \
  --app-dir tests/browser --host 127.0.0.1 --port "$port" --log-level warning >"$fixture_root/server.log" 2>&1 &
fixture_pid=$!
trap 'kill "$fixture_pid" 2>/dev/null || true; wait "$fixture_pid" 2>/dev/null || true' EXIT
ready=false
for ((i=0; i<100; i++)); do
  if ! kill -0 "$fixture_pid" 2>/dev/null; then cat "$fixture_root/server.log"; exit 1; fi
  if curl -fsS "http://127.0.0.1:$port/api/runs/browser-campaign/research-workflow" >/dev/null 2>&1; then ready=true; break; fi
  sleep 0.1
done
if [[ "$ready" != true ]]; then cat "$fixture_root/server.log"; exit 1; fi
LOOPLAB_BROWSER_FIXTURE_URL="http://127.0.0.1:$port" node tests/browser/research_smoke.mjs
