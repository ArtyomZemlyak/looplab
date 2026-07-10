"""The TUI's HTTP client — the `Api` JSON client and its `ApiError`, split verbatim out of
`serve/tui.py` (docs/15 §P5.2) so the transport layer is readable/testable apart from the
interactive REPL. `serve/tui.py` re-exports both names, so `looplab.serve.tui.Api` keeps working."""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# The one looplab import this module allows itself: the wire-protocol vocabulary it shares with the
# server (job statuses). Everything else stays stdlib so the TUI adds no dependencies.
from looplab.serve.protocol import JOB_DONE, JOB_RUNNING, JOB_UNKNOWN

# ----------------------------------------------------------------------------- HTTP client

class ApiError(Exception):
    """A non-2xx response (or transport failure). `.detail` carries the server's human reason when
    FastAPI returned one (it puts it in `detail`), so callers can show "n_seeds: …" not just "422"."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.detail = message
        self.status = status


class Api:
    """Minimal JSON client for the LoopLab UI server — the stdlib mirror of ui/src/util.js. Sends the
    `X-LoopLab-Token` header when LOOPLAB_UI_TOKEN is set (token-gated deployments), exactly like the
    browser does, so the TUI works behind the same auth."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.token = token or os.environ.get("LOOPLAB_UI_TOKEN") or ""
        self.timeout = timeout

    def _headers(self, body: bool = False) -> dict:
        h = {"Accept": "application/json"}
        if body:
            h["Content-Type"] = "application/json"
        if self.token:
            h["X-LoopLab-Token"] = self.token
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self._headers(body is not None))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = (json.loads(e.read().decode("utf-8", errors="replace")) or {}).get("detail", "")
            except Exception:  # noqa: BLE001 - no/garbled JSON body -> fall back to the status line
                pass
            raise ApiError(detail or f"{path}: HTTP {e.code}", status=e.code) from e
        except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException) as e:
            raise ApiError(f"could not reach {self.base}{path}: {e}") from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            # A 200 with a non-JSON body (some other service bound to our port) must surface as an
            # ApiError callers already handle, not an uncaught JSONDecodeError that kills the TUI.
            raise ApiError(f"{path}: invalid JSON from server") from e

    def get(self, path: str, timeout: Optional[float] = None) -> Any:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        return self._request("POST", path, body=body or {}, timeout=timeout)

    def ping(self) -> bool:
        """Is a LoopLab server answering here? (a cheap, short-timeout GET on the runs list)."""
        try:
            self.get("/api/runs", timeout=2.5)
            return True
        except ApiError:
            return False

    # ---- background-job polling: the server hands back {status:'running', job_id} for slow agent work
    # (genesis boss / action-router) so it can't 504 behind a proxy. We poll to completion. Mirrors
    # util.js genesisAwait / jobAwait. Transient poll errors are tolerated (keep polling).
    def _await_job(self, resp: Any, poll_path, *, interval: float, deadline_s: float) -> Any:
        if not isinstance(resp, dict) or resp.get("status") != JOB_RUNNING or not resp.get("job_id"):
            return resp                                     # fast path: already the final result
        deadline = time.monotonic() + deadline_s
        misses = 0
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                j = self.get(poll_path(resp["job_id"]))
                misses = 0
            except ApiError as e:
                # A 5xx (status set) is treated as transient — keep polling. A transport failure
                # (status None: connection refused/timeout) usually means the server died; bail fast
                # after a few in a row instead of spinning out the whole 5-10 min deadline.
                if e.status is None:
                    misses += 1
                    if misses >= 5:
                        return {"ok": False, "error": "lost contact with the server"}
                continue
            if isinstance(j, dict) and j.get("status") == JOB_DONE:
                return j
            if isinstance(j, dict) and j.get("status") == JOB_UNKNOWN:
                return {"ok": False, "error": "the job expired — try again"}
        return {"ok": False, "error": "timed out waiting for the boss"}

    def genesis(self, messages: list, instruction: str, draft: Optional[dict]) -> dict:
        resp = self.post("/api/genesis", {"messages": messages, "instruction": instruction, "draft": draft},
                         timeout=60)
        return self._await_job(resp, lambda j: f"/api/genesis/{j}", interval=1.5, deadline_s=300)

    def command(self, run_id: str, messages: list, instruction: str, node_id=None) -> dict:
        resp = self.post(f"/api/runs/{run_id}/command",
                         {"messages": messages, "instruction": instruction, "node_id": node_id}, timeout=60)
        return self._await_job(resp, lambda j: f"/api/jobs/{j}", interval=1.5, deadline_s=600)
