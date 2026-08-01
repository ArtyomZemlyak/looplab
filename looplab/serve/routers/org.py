"""Run-organization routes (ClearML-style projects, super-tasks, run labels, run delete). Handler
bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from looplab.serve.deletion_service import (
    begin_or_resume_run_deletion, get_run_deletion, validate_deletion_request)
from looplab.serve.projects import ProjectError, ProjectStoreLockError


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, projects = srv.run_dir, srv.projects

    async def _json_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        return body

    # ------------------------------------------------------------------ projects (ClearML-style)
    # CLAUDE REVIEW: [PERF] Every project MUTATOR route that calls this (create_project,
    # patch_project, assign_run, create_supertask, patch_supertask, assign_supertask, rename_run) is
    # an `async def`, so `_project_call(fn)` runs `fn()` — `ProjectStore._transaction` ->
    # `_interprocess_lock(required=True)` -> a BLOCKING `fcntl.flock(LOCK_EX)` with NO timeout, plus
    # load/atomic-save disk I/O — directly on the ASGI event loop. If another UI worker/process holds
    # `projects.json.lock` the flock waits UNBOUNDED, freezing every concurrent SSE tick/poll on this
    # worker. The read routes and the destructive ones (delete_project/delete_supertask/delete_run)
    # are sync `def` (threadpool) and are safe; only these async mutators block. They need the same
    # `anyio.to_thread.run_sync` offload that /control and submit_command already use.
    def _project_call(fn):
        """Map invalid mutations to 400 and an unavailable required durability lock to 503."""
        try:
            return fn()
        except ProjectStoreLockError as e:
            raise HTTPException(503, str(e)) from e
        except ProjectError as e:
            raise HTTPException(400, str(e)) from e

    def _run_project_call(run_id: str, operation: str, fn):
        """Serialize run-id metadata with reset/delete so a stale request cannot resurrect it."""
        rd = _run_dir(run_id)
        with srv.commands.sequence(rd):
            # Re-enter the canonical helper after waiting: deletion can publish its root fence or
            # quarantine the directory between the route's first lookup and sequencer acquisition.
            canonical = _run_dir(run_id)
            if canonical.name != run_id:
                raise HTTPException(409, f"cannot {operation}: run identity changed")
            return _project_call(fn)

    @router.get("/api/projects")
    def list_projects():
        return projects.load()

    @router.post("/api/projects")
    async def create_project(request: Request):
        body = await _json_object(request)
        p = _project_call(lambda: projects.create(body.get("name", ""), body.get("parent_id")))
        return p.model_dump()

    @router.patch("/api/projects/{pid}")
    async def patch_project(pid: str, request: Request):
        body = await _json_object(request)

        def _apply():
            if "name" in body and body["name"] is not None:
                projects.rename(pid, body["name"])
            if "parent_id" in body:
                projects.reparent(pid, body["parent_id"])
        _project_call(_apply)
        return {"ok": True}

    @router.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        _project_call(lambda: projects.delete(pid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/project")
    async def assign_run(run_id: str, request: Request):
        body = await _json_object(request)
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, "assign run", lambda: projects.assign(run_id, body.get("project_id"))))
        return {"ok": True}

    # ------------------------------------------------------------------ super-tasks (flat axis)
    @router.get("/api/supertasks")
    def list_supertasks():
        data = projects.load()
        return {"supertasks": data["supertasks"], "assignments": data["supertask_assignments"]}

    @router.post("/api/supertasks")
    async def create_supertask(request: Request):
        body = await _json_object(request)
        st = _project_call(lambda: projects.create_supertask(body.get("name", ""), body.get("task_id")))
        return st

    @router.patch("/api/supertasks/{sid}")
    async def patch_supertask(sid: str, request: Request):
        body = await _json_object(request)
        _project_call(lambda: projects.rename_supertask(sid, body.get("name", "")))
        return {"ok": True}

    @router.delete("/api/supertasks/{sid}")
    def delete_supertask(sid: str):
        _project_call(lambda: projects.delete_supertask(sid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/supertask")
    async def assign_supertask(run_id: str, request: Request):
        body = await _json_object(request)
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, "assign super-task",
            lambda: projects.assign_supertask(run_id, body.get("supertask_id"))))
        return {"ok": True}

    @router.patch("/api/runs/{run_id}")
    async def rename_run(run_id: str, request: Request):
        """Set/clear a run's UI display label. Non-destructive: the run dir id is unchanged."""
        body = await _json_object(request)
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, "rename run", lambda: projects.set_label(run_id, body.get("label"))))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/deletions")
    async def create_run_deletion(run_id: str, request: Request):
        """Delete one exact run generation through an operation-bound durable transaction."""
        operation_id, generation, expected_seq = validate_deletion_request(
            await _json_object(request))
        result = await anyio.to_thread.run_sync(lambda: begin_or_resume_run_deletion(
            srv, run_id, operation_id=operation_id,
            expected_generation=generation, expected_seq=expected_seq))
        return JSONResponse(
            status_code=200 if result.get("status") == "succeeded" else 202,
            content=result)

    @router.get("/api/runs/{run_id}/deletions/{operation_id}")
    def observe_run_deletion(run_id: str, operation_id: str):
        return get_run_deletion(srv, run_id, operation_id)

    @router.delete("/api/runs/{run_id}")
    def legacy_delete_run(run_id: str):
        """Never let a bodyless request delete an uninspected replacement generation."""
        raise HTTPException(409, {
            "code": "deletion_identity_required",
            "message": "Run deletion requires an exact generation, tail, and operation id.",
            "remediation": (
                f"POST /api/runs/{run_id}/deletions with the inspected snapshot identity."),
        })

    return router
