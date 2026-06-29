"""Launch the LoopLab UI server with LLM env preloaded from .env.dev.

The plain `looplab ui` server reads its LLM settings (base_url + API key) from the environment, but
a preview/launch.json spawn does NOT source .env.dev — so the chat/suggest/health endpoints 401
without the OpenRouter key. This thin shim loads .env.dev (without clobbering anything already set in
the real environment) and then hands off to the normal CLI, so a restart via launch.json stays keyed.
"""
import os
import sys
from pathlib import Path

_envfile = Path(__file__).resolve().parent / ".env.dev"
if _envfile.exists():
    for _line in _envfile.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())   # real env vars win over the file

from looplab.cli import app  # noqa: E402 — import after env is in place

if __name__ == "__main__":
    sys.argv = ["looplab", "ui", "--run-root", "runs", "--port", "8770"]
    app()
