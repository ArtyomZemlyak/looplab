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
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional

# The one looplab import this module allows itself: the wire-protocol vocabulary it shares with the
# server (job statuses). Everything else stays stdlib so the TUI adds no dependencies.
from looplab.serve.protocol import JOB_DONE, JOB_RUNNING, JOB_UNKNOWN


_COMMAND_TERMINAL = frozenset({"succeeded", "noop", "failed", "rejected", "timed_out"})
_COMMAND_PENDING = frozenset({"accepted", "executing"})


def command_error_transient(error: "ApiError") -> bool:
    """HTTP statuses whose response may follow durable server acceptance."""
    status = getattr(error, "status", None)
    return status is None or status in {408, 425, 429} or status >= 500


def _path_segment(value: Any) -> str:
    """Quote one opaque URL path segment (run/command ids may contain spaces or slashes)."""
    return urllib.parse.quote(str(value), safe="")

# ----------------------------------------------------------------------------- HTTP client

class ApiError(Exception):
    """A non-2xx response (or transport failure). `.detail` carries the server's human reason when
    FastAPI returned one (it puts it in `detail`), so callers can show "n_seeds: …" not just "422"."""

    def __init__(self, message: Any, status: Optional[int] = None):
        self.payload = dict(message) if isinstance(message, dict) else None
        self.code = str(self.payload.get("code") or "") if self.payload else ""
        self.existing_command_id = (str(self.payload.get("existing_command_id") or "")
                                    if self.payload else "")
        if self.payload:
            lead = str(self.payload.get("message") or self.code or "request failed")
            command = (f" (command {self.existing_command_id})"
                       if self.existing_command_id else "")
            remediation = str(self.payload.get("remediation") or "")
            text = f"{lead}{command}" + (f"; {remediation}" if remediation else "")
        else:
            text = str(message)
        super().__init__(text)
        self.detail = text
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

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 timeout: Optional[float] = None, headers: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request_headers = self._headers(body is not None)
        request_headers.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=request_headers)
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

    @staticmethod
    def _command_record(record: Any, path: str, *, expected_id: Optional[str] = None) -> dict:
        """Validate the minimum lifecycle envelope before the TUI trusts its status."""
        if not isinstance(record, dict):
            raise ApiError(f"{path}: invalid command response", status=200)
        command_id = record.get("id")
        if not command_id:
            raise ApiError(f"{path}: command response has no id", status=200)
        if expected_id is not None and str(command_id) != str(expected_id):
            raise ApiError(f"{path}: command response id does not match the requested command", status=200)
        status = record.get("status")
        if status not in _COMMAND_TERMINAL and status not in _COMMAND_PENDING:
            raise ApiError(f"{path}: invalid command status {status!r}", status=200)
        return record

    def run_command(self, run_id: str, event_type: str, data: Optional[dict] = None,
                    wait_s: float = 8.0, submit_retries: int = 1,
                    idempotency_key: Optional[str] = None) -> dict:
        """Submit one authoritative run command and briefly follow its observable lifecycle.

        The idempotency key belongs to this logical submission. A lost response or 5xx may replay the
        POST with that *same* key; polling then uses the returned command id. A slow command is not
        reported as successful merely because the POST was accepted; after the bounded client wait it
        comes back as ``executing`` so the TUI can render an honest requested/pending row. Terminal
        command failures are records, not transport exceptions.
        """
        path = f"/api/runs/{_path_segment(run_id)}/commands"
        key = str(idempotency_key or uuid.uuid4())
        record = None
        for attempt in range(max(0, int(submit_retries)) + 1):
            try:
                record = self._request(
                    "POST", path, body={"type": event_type, "data": data or {}},
                    headers={"Idempotency-Key": key})
                break
            except ApiError as exc:
                # A 4xx is an authoritative client/state rejection. Transport failures and 5xx may
                # happen after the durable command was accepted, so replay only those with the same
                # key and let the server return the existing record.
                if (exc.status == 409 and exc.code == "retry_existing_command"
                        and exc.existing_command_id):
                    record = self.get_run_command(run_id, exc.existing_command_id)
                    break
                retryable = command_error_transient(exc)
                if not retryable or attempt >= max(0, int(submit_retries)):
                    raise
                time.sleep(0.15)
        record = self._command_record(record, path)
        if record.get("status") in _COMMAND_TERMINAL:
            return record

        command_id = record.get("id")
        last = record
        deadline = time.monotonic() + max(0.0, float(wait_s))
        try:
            while time.monotonic() < deadline:
                # A short cadence makes quick stop/resume transitions feel immediate without turning the
                # command endpoint into a long-held HTTP request. Only transport failures and 5xx are
                # transient. Auth/not-found responses are authoritative and must not be disguised as an
                # indefinitely executing command.
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                try:
                    refreshed = self.get_run_command(run_id, command_id)
                except ApiError as exc:
                    if command_error_transient(exc):
                        continue
                    raise
                last = refreshed
                if refreshed.get("status") in _COMMAND_TERMINAL:
                    return refreshed
        except KeyboardInterrupt:
            # Ctrl-C stops only the local wait, never an accepted server command.
            pass
        return {**last, "status": "executing"}

    def get_run_command(self, run_id: str, command_id: str) -> dict:
        """Fetch one durable command record using URL-safe opaque identifiers."""
        path = (f"/api/runs/{_path_segment(run_id)}/commands/"
                f"{_path_segment(command_id)}")
        return self._command_record(self.get(path), path, expected_id=str(command_id))

    def refresh_report(self, run_id: str) -> dict:
        """Regenerate a report through its dedicated background-job endpoint (not run commands)."""
        resp = self.post(f"/api/runs/{_path_segment(run_id)}/report_refresh", {}, timeout=60)
        return self._await_job(resp, lambda j: f"/api/jobs/{j}", interval=1.5, deadline_s=600)

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
        resp = self.post(f"/api/runs/{_path_segment(run_id)}/command",
                         {"messages": messages, "instruction": instruction, "node_id": node_id}, timeout=60)
        return self._await_job(resp, lambda j: f"/api/jobs/{j}", interval=1.5, deadline_s=600)
