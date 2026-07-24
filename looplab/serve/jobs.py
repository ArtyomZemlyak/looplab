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
from threading import Lock as _ThreadLock

import anyio
from fastapi import APIRouter

from looplab.serve.protocol import JOB_DONE, JOB_RUNNING, JOB_UNKNOWN


_MAX_JOBS = 64
# Browser and TUI poll contracts wait up to ten minutes. Never evict a completed receipt while a
# conforming client can still be waiting for it; bounded capacity fails closed instead.
_COMPLETED_RETENTION_SECONDS = 660.0


# ---- generic background-job registry (generalizes the genesis pattern) -----------------------
# Slow, unbounded work (an agent synthesizing across many runs, a heavy report regen) must not run
# inline: behind a UI proxy (JupyterHub's jupyter-server-proxy) a request that outlasts the gateway
# timeout 504s and the work is lost. A handler hands the work to a worker thread, waits briefly
# inline (a fast/offline result still returns in the one request — no polling, no added latency),
# then returns {status:'running', job_id} the UI polls via GET /api/jobs/{job_id}.
class JobRegistry:
    def __init__(self):
        self._jobs: dict = {}
        self._idempotent_jobs: dict[str, str] = {}
        self._job_identities: dict[str, str] = {}
        self._lock = threading.Lock()
        self._inline_wait = float(os.environ.get("LOOPLAB_JOB_INLINE_WAIT", "8.0"))

    def _remove_locked(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        identity = self._job_identities.pop(job_id, None)
        if identity is not None and self._idempotent_jobs.get(identity) == job_id:
            self._idempotent_jobs.pop(identity, None)

    def _make_room_locked(self, now: float) -> bool:
        """Evict only old completed receipts; running work and fresh results are never displaced."""
        if len(self._jobs) < _MAX_JOBS:
            return True
        completed = sorted(
            (job_id for job_id, job in self._jobs.items()
             if job.get("status") == JOB_DONE
             and now - float(job.get("ts", now)) >= _COMPLETED_RETENTION_SECONDS),
            key=lambda job_id: self._jobs[job_id].get("ts", 0),
        )
        for job_id in completed:
            self._remove_locked(job_id)
            if len(self._jobs) < _MAX_JOBS:
                return True
        return False

    def put(self, job_id: str, **fields) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {}).update(fields)

    def get(self, job_id: str):
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def poll(self, job_id: str):
        """Read a public poll receipt, retiring an explicitly consumable terminal atomically."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            receipt = dict(job)
            # One-shot polling is safe only when the endpoint owns durable replay.
            if job.get("status") == JOB_DONE and job.get("consume_on_poll"):
                self._remove_locked(job_id)
            return receipt

    def _completed_result(self, job_id: str, *, consume: bool):
        """Atomically observe a terminal result and optionally retire its private receipt.

        A non-idempotent job that completes during ``run_as_job``'s inline wait never publishes its
        ``job_id`` to a caller, so retaining that unreachable receipt only burns shared capacity.
        Idempotent jobs are not consumed by default; only callers with an independent durable replay
        ledger may opt in after their compute path has published its terminal receipt.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != JOB_DONE:
                return False, None
            result = job.get("result")
            if consume:
                self._remove_locked(job_id)
            return True, result

    def rejoin(self, identity: str) -> dict | None:
        """Return an existing process receipt without reserving or evicting replacement work."""
        with self._lock:
            job_id = self._idempotent_jobs.get(identity)
            if not job_id or job_id not in self._jobs:
                return None
            return {"status": JOB_RUNNING, "job_id": job_id}

    def has_identity(self, identity: str) -> bool:
        """Whether this process can still rejoin the exact in-flight/done logical job."""
        return self.rejoin(identity) is not None

    @property
    def inline_wait(self) -> float:
        return self._inline_wait

    def reserve(self, idempotency_key: str | None = None, *,
                consume_on_poll: bool = False) -> dict:
        """Atomically reserve/rejoin a bounded job identity without starting its worker.

        ``consume_on_poll`` is fixed by the first reservation and is only for endpoints whose
        durable ledger can replay a terminal response after the process-local receipt is retired.
        A rejoin never changes the policy attached to the exact existing receipt.
        """
        with self._lock:
            job_id = (self._idempotent_jobs.get(idempotency_key)
                      if idempotency_key is not None else None)
            if job_id is not None and job_id in self._jobs:
                return {"status": JOB_RUNNING, "job_id": job_id}
            if idempotency_key is not None:
                self._idempotent_jobs.pop(idempotency_key, None)
            now = time.time()
            if not self._make_room_locked(now):
                return {
                    "ok": False,
                    "code": "job_capacity",
                    "error_kind": "capacity",
                    "error": "background job capacity is temporarily full; retry later",
                }
            job_id = secrets.token_hex(8)
            self._jobs[job_id] = {
                "status": JOB_RUNNING, "result": None, "ts": now, "worker_started": False,
                "consume_on_poll": bool(consume_on_poll),
            }
            if idempotency_key is not None:
                self._idempotent_jobs[idempotency_key] = job_id
                self._job_identities[job_id] = idempotency_key
            return {"status": JOB_RUNNING, "job_id": job_id}

    def discard_reservation(self, job_id: str) -> None:
        """Release only a workerless reservation whose durable caller-side claim failed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and not job.get("worker_started"):
                self._remove_locked(job_id)

    def mark_consumable(self, job_id: str) -> None:
        """Allow retirement only after an endpoint confirmed its independent durable terminal."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["consume_on_poll"] = True

    def discard_orphaned_running(self, job_id: str) -> None:
        """Retire an exact running receipt after its owner proved the worker cannot still exist.

        This is deliberately narrower than cancellation: the background registry cannot prove thread
        liveness itself. Durable endpoints may call it only after their cross-process lease is dead and
        a strict indeterminate tombstone has replaced the paid-action claim.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.get("status") == JOB_RUNNING:
                self._remove_locked(job_id)

    def start_reserved(self, job_id: str, compute, *, with_progress: bool = False,
                       on_start_failure=None) -> None:
        """Start an existing reservation once; safe for same-identity concurrent callers."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("worker_started") or job.get("status") == JOB_DONE:
                return
            job["worker_started"] = True

        launch_lock = _ThreadLock()
        launch_state = "pending"

        def _worker():
            # ``Thread.start`` is allowed to fail only before a target begins, but custom
            # runtimes can violate that convention by starting and then raising. Never let paid work
            # cross this boundary until start() returned and authorization was atomically committed.
            with launch_lock:
                authorized = launch_state == "authorized"
            if not authorized:
                return
            try:
                res = compute(lambda p: self.put(job_id, progress=p)) if with_progress else compute()
            except Exception:  # noqa: BLE001 - raw provider/internal errors must never cross /api/jobs
                res = {
                    "ok": False,
                    "code": "job_failed",
                    "error_kind": "internal",
                    "error": "background job failed",
                }
            self.put(job_id, status=JOB_DONE, result=res, ts=time.time())

        start_exc: BaseException | None = None
        with launch_lock:
            try:
                threading.Thread(target=_worker, daemon=True).start()
                # Authorization belongs inside this protected region: an asynchronous exception
                # anywhere after start() returned but before this commit must still cancel the target.
                launch_state = "authorized"
            except BaseException as exc:
                # The target, if a non-conforming start() already launched it, is blocked on this
                # lock until the cancelled state is visible and therefore cannot call the provider.
                launch_state = "cancelled"
                start_exc = exc

        if start_exc is not None:
            callback_exc: BaseException | None = None
            try:
                result = on_start_failure() if on_start_failure is not None else {
                    "ok": False,
                    "code": "job_failed",
                    "error_kind": "internal",
                    "error": "background job failed",
                }
            except BaseException as exc:
                callback_exc = exc
                result = {
                    "ok": False,
                    "code": "job_failed",
                    "error_kind": "internal",
                    "error": "background job failed",
                }
            self.put(job_id, status=JOB_DONE, result=result, ts=time.time())
            if not isinstance(start_exc, Exception):
                raise start_exc.with_traceback(start_exc.__traceback__)
            if callback_exc is not None and not isinstance(callback_exc, Exception):
                raise callback_exc

    async def run_reserved(self, job_id: str, compute, *, inline_wait: float | None = None,
                           with_progress: bool = False, consume: bool = False,
                           on_start_failure=None):
        """Start/wait for an exact receipt; never create replacement work if it disappeared."""
        self.start_reserved(
            job_id, compute, with_progress=with_progress,
            on_start_failure=on_start_failure)
        deadline = time.monotonic() + (self._inline_wait if inline_wait is None else inline_wait)
        while time.monotonic() < deadline:
            done, result = self._completed_result(job_id, consume=consume)
            if done:
                return result
            await anyio.sleep(0.2)
        if self.get(job_id) is None:
            return {
                "ok": False,
                "code": "job_unknown",
                "ambiguous": True,
                "error": "background job receipt is unavailable",
            }
        return {"status": JOB_RUNNING, "job_id": job_id}

    async def run_as_job(self, compute, *, inline_wait: float | None = None,
                         with_progress: bool = False,
                         idempotency_key: str | None = None,
                         consume_inline_result: bool = False,
                         reserved_job_id: str | None = None,
                         on_start_failure=None):
        """Run `compute` (a 0-arg callable returning the final response dict) in a worker thread; return
        its result inline when it finishes within the inline wait, else {status:'running', job_id}. The
        thread keeps a blocking LLM/agent call off the event loop AND off the request's critical path,
        so it can't stall other clients or 504 behind a proxy.

        `with_progress=True` hands `compute` ONE argument instead — a `set_progress(payload)` callable
        that annotates the running job's `progress` field (thread-safe; genesis streams its live scout
        steps through it, and its poll endpoint surfaces them). `inline_wait` overrides the registry-wide
        default for this one call (genesis keeps its own LOOPLAB_GENESIS_INLINE_WAIT knob).

        ``consume_inline_result`` is for an idempotent caller whose compute path publishes an
        independent durable replay receipt before returning. When such work finishes inline, its
        process-local receipt is private and can be retired just like a non-idempotent inline job;
        generic idempotent callers keep the historical in-memory rejoin behavior by default.

        ``reserved_job_id`` is the rejoin-only path for a caller that already bound a durable claim
        to an exact process receipt. It never falls back to allocating replacement work.
        """
        if reserved_job_id is not None:
            return await self.run_reserved(
                reserved_job_id, compute, inline_wait=inline_wait,
                with_progress=with_progress, consume=consume_inline_result,
                on_start_failure=on_start_failure)
        receipt = self.reserve(idempotency_key)
        if receipt.get("status") != JOB_RUNNING:
            return receipt
        return await self.run_reserved(
            receipt["job_id"], compute, inline_wait=inline_wait,
            with_progress=with_progress,
            consume=idempotency_key is None or consume_inline_result,
            on_start_failure=on_start_failure)


def build_router(srv) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        """Poll a generic background job (see _run_as_job): `running` until done, then the result dict
        with status='done'; `unknown` if its volatile process receipt expired or was evicted. The
        caller must use its endpoint-specific durable identity to reconcile unknown outcomes."""
        j = srv.jobs.poll(job_id)
        if not j:
            return {"status": JOB_UNKNOWN}
        if j.get("status") != JOB_DONE:
            return {"status": JOB_RUNNING}
        return {**j["result"], "status": JOB_DONE}

    return router
