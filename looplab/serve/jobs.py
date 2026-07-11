"""Generic background-job registry for the UI server (BACKLOG §4 extraction; bodies verbatim from
`serve/server.py`). One `JobRegistry` per app — genesis, the boss command route, report refresh and
the scope reports all share it, so ONE store backs both `/api/jobs/{id}` and `/api/genesis/{id}`
(genesis rides through `run_as_job(with_progress=True)`, writing a `progress` field its poll
endpoint surfaces)."""
from __future__ import annotations

import os
import secrets
import threading
import time

import anyio
from fastapi import APIRouter

from looplab.serve.protocol import JOB_DONE, JOB_RUNNING, JOB_UNKNOWN


# ---- generic background-job registry (generalizes the genesis pattern) -----------------------
# Slow, unbounded work (an agent synthesizing across many runs, a heavy report regen) must not run
# inline: behind a UI proxy (JupyterHub's jupyter-server-proxy) a request that outlasts the gateway
# timeout 504s and the work is lost. A handler hands the work to a worker thread, waits briefly
# inline (a fast/offline result still returns in the one request — no polling, no added latency),
# then returns {status:'running', job_id} the UI polls via GET /api/jobs/{job_id}.
class JobRegistry:
    def __init__(self):
        self._jobs: dict = {}
        self._lock = threading.Lock()
        self._inline_wait = float(os.environ.get("LOOPLAB_JOB_INLINE_WAIT", "8.0"))

    def put(self, job_id: str, **fields) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {}).update(fields)
            if len(self._jobs) > 64:     # bound memory: keep the most-recent 64
                for k in sorted(self._jobs, key=lambda j: self._jobs[j].get("ts", 0))[:-64]:
                    self._jobs.pop(k, None)

    def get(self, job_id: str):
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    async def run_as_job(self, compute, *, inline_wait: float | None = None, with_progress: bool = False):
        """Run `compute` (a 0-arg callable returning the final response dict) in a worker thread; return
        its result inline when it finishes within the inline wait, else {status:'running', job_id}. The
        thread keeps a blocking LLM/agent call off the event loop AND off the request's critical path,
        so it can't stall other clients or 504 behind a proxy.

        `with_progress=True` hands `compute` ONE argument instead — a `set_progress(payload)` callable
        that annotates the running job's `progress` field (thread-safe; genesis streams its live scout
        steps through it, and its poll endpoint surfaces them). `inline_wait` overrides the registry-wide
        default for this one call (genesis keeps its own LOOPLAB_GENESIS_INLINE_WAIT knob)."""
        job_id = secrets.token_hex(8)
        self.put(job_id, status=JOB_RUNNING, result=None, ts=time.time())

        def _worker():
            try:
                res = compute(lambda p: self.put(job_id, progress=p)) if with_progress else compute()
            except Exception as e:  # noqa: BLE001 - surface a usable error, never crash the worker
                res = {"ok": False, "error": str(e)}
            self.put(job_id, status=JOB_DONE, result=res, ts=time.time())
        threading.Thread(target=_worker, daemon=True).start()

        deadline = time.monotonic() + (self._inline_wait if inline_wait is None else inline_wait)
        while time.monotonic() < deadline:
            j = self.get(job_id)
            if j and j.get("status") == JOB_DONE:
                return j["result"]
            await anyio.sleep(0.2)
        return {"status": JOB_RUNNING, "job_id": job_id}


def build_router(srv) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        """Poll a generic background job (see _run_as_job): `running` until done, then the result dict
        with status='done'; `unknown` if it expired/was evicted (the UI should re-issue the action)."""
        j = srv.jobs.get(job_id)
        if not j:
            return {"status": JOB_UNKNOWN}
        if j.get("status") != JOB_DONE:
            return {"status": JOB_RUNNING}
        return {**j["result"], "status": JOB_DONE}

    return router
