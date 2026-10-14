"""Cross-run orchestration for external agents: waking (agent events + hook) and GPU order.

Routes (all JSON; the harness token may use every one except the operator-only hook writes):

* `GET  /api/agent-events` — the long-poll over the durable outbox (`serve/agent_events.py`).
* `GET  /api/agent-notify` / `PUT` / `DELETE` / `POST /api/agent-notify/test` — the operator's
  notification hook and its delivery ledger. Writing the COMMAND is operator-only: it is code the
  server executes.
* `GET  /api/runs/{run_id}/agent-notify` / `PUT` — one run's recent events, ledger rows and its
  hook variables (`thread`, …) + on/off switch; the harness token may write these.
* `GET  /api/gpu-queue` — who holds the host GPU pool, who waits in FIFO order, and every run's
  GPU gate (`engine/gpu_queue.py`).
* `GET  /api/runs/{run_id}/gpu-gate` / `PUT` — hold a run's GPU work, or start it only after other
  runs' engines have exited.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from looplab.serve.principal import request_agent_token


class GpuGateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold: bool = False
    start_after: list[str] = Field(default_factory=list, max_length=16)
    reason: str = Field(default="", max_length=500)
    clear: bool = False


class RunNotifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vars: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None


def _owner_only(request: Request, what: str) -> None:
    if request_agent_token(request):
        raise HTTPException(403, f"the harness token cannot {what}; ask the operator "
                                 "(`looplab agent-notify` or the owner UI)")


def build_router(srv) -> APIRouter:
    router = APIRouter()

    def _pump():
        return getattr(srv, "agent_events", None)

    # ------------------------------------------------------------------ agent events (long poll)
    @router.get("/api/agent-events")
    async def get_agent_events(response: Response, after: Optional[int] = Query(None, ge=-1),
                               timeout: float = Query(0.0, ge=0.0, le=600.0),
                               run_id: Optional[str] = Query(None, max_length=255),
                               kinds: Optional[str] = Query(None, max_length=400),
                               limit: int = Query(100, ge=1, le=500)):
        """Actionable events LoopLab recorded for external agents, oldest first.

        `after` is the cursor: the last event id you processed. Omitted, the call subscribes
        "from now" (no events; `next_after` is the newest id). `-1` reads from the start of the
        retained outbox. `timeout` (seconds, <= 600) turns the read into a long poll that returns
        as soon as an event past the cursor exists. `run_id` keeps that run's events plus host
        events (`gpu_free`); `kinds` is a comma list. An event is a nudge: read the run's
        harness-progress before acting. `stale_cursor` means the outbox was rebuilt — continue
        from `next_after`; `gap` means older events rotated away."""
        from looplab.serve.agent_events import ACTIONABLE_KINDS, read_outbox, wait_for_events

        response.headers["Cache-Control"] = "no-store"
        wanted = None
        if kinds:
            wanted = [k.strip() for k in kinds.split(",") if k.strip()]
            bad = [k for k in wanted if k not in ACTIONABLE_KINDS]
            if bad:
                raise HTTPException(400, f"unknown kinds {bad}; choose from {list(ACTIONABLE_KINDS)}")
        if run_id is not None:
            srv.run_dir(run_id)          # 404 for an unknown run, before any wait
        if after is None:
            page = await anyio.to_thread.run_sync(
                lambda: read_outbox(srv.root, after=10**18, run_id=run_id, kinds=wanted, limit=1))
            return {"events": [], "next_after": page["newest_id"], "newest_id": page["newest_id"],
                    "has_more": False, "timed_out": False, "subscribed_from_now": True}
        page = await anyio.to_thread.run_sync(
            lambda: wait_for_events(_pump(), srv.root, after=after, timeout=timeout,
                                    run_id=run_id, kinds=wanted, limit=limit),
            abandon_on_cancel=True)
        return page

    # ------------------------------------------------------------------ notification hook
    @router.get("/api/agent-notify")
    def get_agent_notify(response: Response):
        """The operator's notification hook, its delivery watermark and the recent ledger."""
        from looplab.serve.agent_events import describe_event_kinds, notify_status

        response.headers["Cache-Control"] = "no-store"
        return {**notify_status(srv.root), "kinds": describe_event_kinds()}

    @router.put("/api/agent-notify")
    async def put_agent_notify(request: Request):
        """Configure the hook: `{command: [argv...] | "string", vars: {...}, debounce_s, ...}`.

        Operator only. The command runs without a shell; placeholders are `{message}`,
        `{run_id}`, `{runs}`, `{kinds}`, `{count}`, `{after}`, `{first_id}`, `{last_id}` and
        every name in `vars` (a run may override vars via its own agent-notify)."""
        from looplab.serve.agent_events import write_notify_config

        _owner_only(request, "configure the notification command")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "body must be JSON") from exc
        try:
            return {"ok": True, "config": await anyio.to_thread.run_sync(
                lambda: write_notify_config(srv.root, body))}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/agent-notify")
    def delete_agent_notify(request: Request):
        """Remove the hook (the outbox and ledger stay)."""
        from looplab.serve.agent_events import clear_notify_config

        _owner_only(request, "remove the notification command")
        clear_notify_config(srv.root)
        return {"ok": True}

    @router.post("/api/agent-notify/test")
    async def test_agent_notify(request: Request):
        """Run the configured command once with a test message; the result lands in the ledger."""
        from looplab.serve.agent_events import (_append_ledger, read_notify_config, render_argv)
        import time as _time

        _owner_only(request, "run the notification command")
        config = read_notify_config(srv.root)
        if not config:
            raise HTTPException(409, "no notification hook is configured")
        pump = _pump()
        runner = pump.runner if pump is not None else None
        if runner is None:
            from looplab.serve.agent_events import AgentEventPump
            runner = AgentEventPump._run_command
        fields = {**config.get("vars", {}), "message": "LoopLab test notification (no events).",
                  "run_id": "", "runs": "", "kinds": "test", "count": "0", "after": "-1",
                  "first_id": "-1", "last_id": "-1"}
        try:
            argv = render_argv(config["command"], fields)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"template: {exc}") from exc
        result = await anyio.to_thread.run_sync(
            lambda: runner(argv, float(config.get("timeout_s", 60))))
        row = {"ts": _time.time(), "attempt": 1, "event_ids": [], "runs": [], "kinds": ["test"],
               "status": "delivered" if result["ok"] else "failed",
               "exit_code": result.get("exit_code"), "error": str(result.get("error") or "")[:400],
               "command": config["command"][0], "test": True}
        _append_ledger(srv.root, row)
        return row

    @router.get("/api/runs/{run_id}/agent-notify")
    def get_run_agent_notify(run_id: str, response: Response):
        """This run's recent actionable events, its delivery ledger rows and hook variables."""
        from looplab.serve.agent_events import run_event_summary

        response.headers["Cache-Control"] = "no-store"
        srv.run_dir(run_id)
        return run_event_summary(srv.root, run_id)

    @router.put("/api/runs/{run_id}/agent-notify")
    def put_run_agent_notify(run_id: str, body: RunNotifyBody):
        """Set this run's hook variables (e.g. `{"thread": "<id>"}`) or switch its notifications
        on/off. Values are short tokens; the command itself is the operator's."""
        from looplab.serve.agent_events import write_run_notify

        rd = srv.run_dir(run_id)
        try:
            return {"ok": True, "notify": write_run_notify(rd, variables=body.vars,
                                                           enabled=body.enabled)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------------ earlier attempts' logs
    @router.get("/api/runs/{run_id}/attempt-logs")
    def get_attempt_logs(run_id: str, response: Response,
                         node_id: Optional[int] = Query(None, ge=0)):
        """Logs of earlier lifecycles, kept when a node's workdir was rebuilt (reset/retry).

        Each file's `path` is readable through `GET /api/runs/{run_id}/artifact?root=run&path=...`
        with the current expected_generation."""
        from looplab.engine.attempt_logs import list_attempt_logs

        response.headers["Cache-Control"] = "no-store"
        return {"run_id": run_id, "archives": list_attempt_logs(srv.run_dir(run_id), node_id)}

    @router.get("/api/runs/{run_id}/nodes/{node_id}/failure-forensics")
    def get_failure_forensics(run_id: str, node_id: int, response: Response):
        """Every recorded forensic record of a node's failed attempts (newest first) — the
        `failure_forensics` on its `node_failed` / `node_repaired` rows — plus the logs of its
        earlier lifecycles kept across resets."""
        from looplab.engine.attempt_logs import list_attempt_logs
        from looplab.events.types import EV_NODE_FAILED, EV_NODE_REPAIRED

        response.headers["Cache-Control"] = "no-store"
        rd = srv.run_dir(run_id)
        attempts = []
        for ev in srv.events(rd):
            if ev.type not in (EV_NODE_FAILED, EV_NODE_REPAIRED):
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("node_id") != node_id or not isinstance(data.get("failure_forensics"), dict):
                continue
            attempts.append({"seq": ev.seq, "event": ev.type, "generation": data.get("generation"),
                             "attempt": data.get("attempt"),
                             "forensics": data["failure_forensics"]})
        return {"run_id": run_id, "node_id": node_id, "attempts": attempts[::-1],
                "attempt_logs": list_attempt_logs(rd, node_id)}

    # ------------------------------------------------------------------ GPU queue and gates
    def _gates() -> list[dict]:
        from looplab.engine.gpu_queue import gate_blockers, read_gate

        rows = []
        try:
            children = sorted(p for p in Path(srv.root).iterdir()
                              if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return rows
        for rd in children:
            gate = read_gate(rd)
            if gate is None:
                continue
            rows.append({"run_id": rd.name, **gate,
                         "start_after_ids": [Path(p).name for p in gate.get("start_after", [])],
                         "blockers": gate_blockers(gate)})
        return rows

    @router.get("/api/gpu-queue")
    def get_gpu_queue(response: Response):
        """Who holds the host GPU pool, who waits for it (FIFO order) and every run's GPU gate."""
        from looplab.engine import resources
        from looplab.engine.gpu_queue import queue_status

        response.headers["Cache-Control"] = "no-store"
        status = queue_status(resources.default_gpu_host_lease_path())
        return {**status, "gates": _gates()}

    @router.get("/api/runs/{run_id}/gpu-gate")
    def get_gpu_gate(run_id: str, response: Response):
        """This run's GPU gate and what it still waits for (empty `blockers` = open)."""
        from looplab.engine.gpu_queue import gate_blockers, read_gate

        response.headers["Cache-Control"] = "no-store"
        rd = srv.run_dir(run_id)
        gate = read_gate(rd)
        return {"run_id": run_id, "gate": gate, "blockers": gate_blockers(gate)}

    @router.put("/api/runs/{run_id}/gpu-gate")
    def put_gpu_gate(run_id: str, body: GpuGateBody):
        """Hold this run's GPU admission, or start it only after other runs' engines exit.

        `{"hold": true}` takes no GPU until a later PUT with `hold: false`; `{"start_after":
        ["run-a"]}` waits until run-a's engine is no longer running (paused, finished, aborted or
        crashed). `{"clear": true}` removes the gate. CPU-only work is never gated. The engine
        re-reads the gate on every admission poll; a cycle of start_after edges is refused."""
        from looplab.engine.gpu_queue import clear_gate, gate_blockers, read_gate, write_gate

        rd = srv.run_dir(run_id)
        if body.clear:
            clear_gate(rd)
            return {"run_id": run_id, "gate": None, "blockers": []}
        targets = []
        for other in body.start_after:
            if other == run_id:
                raise HTTPException(400, "a run cannot start after itself")
            targets.append(srv.run_dir(other))          # 404 for an unknown run
        edges = {g["run_id"]: set(g["start_after_ids"]) for g in _gates()}
        edges[run_id] = {Path(t).name for t in targets}
        if _has_cycle(edges):
            raise HTTPException(409, "start_after would create a cycle; no run could ever start")
        gate = write_gate(rd, hold=body.hold, start_after=[str(t) for t in targets],
                          reason=body.reason, updated_by="api")
        return {"run_id": run_id, "gate": read_gate(rd) or gate,
                "blockers": gate_blockers(read_gate(rd))}

    return router


def _has_cycle(edges: dict[str, set[str]]) -> bool:
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = GREY
        for nxt in edges.get(node, ()):
            c = color.get(nxt, WHITE)
            if c == GREY or (c == WHITE and visit(nxt)):
                return True
        color[node] = BLACK
        return False

    return any(color.get(n, WHITE) == WHITE and visit(n) for n in list(edges))
