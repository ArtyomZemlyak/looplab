"""Preview-only LoopLab UI launcher (port 8771).

Same as ui_with_env.py but on a dedicated port so a design/UX review session can run its own
preview server alongside another chat's :8770 instance. Reads .env.dev (without clobbering the real
env) so chat/health endpoints stay keyed, then serves the built UI + the real runs/ data read-only.
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
    sys.argv = ["looplab", "ui", "--run-root", "runs", "--port", "8771"]
    app()
