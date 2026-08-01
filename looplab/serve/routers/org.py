"""Run-organization routes (ClearML-style projects, super-tasks, run labels, run delete). Handler
bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import re

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from looplab.serve.deletion_service import (
    begin_or_resume_run_deletion, get_run_deletion, validate_deletion_request)
from looplab.serve.projects import ProjectConflictError, ProjectError, ProjectStoreLockError
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD


_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


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
    def _project_call(fn):
        """Map CAS conflicts to 409, invalid mutations to 400, and lock failures to 503.

        BLOCKING: `fn` reaches `ProjectStore._transaction` -> `_interprocess_lock(required=True)` ->
        an unbounded `fcntl.flock(LOCK_EX)`, plus load/atomic-save disk I/O. A sync `def` route runs
        in FastAPI's threadpool and may call this directly; an `async def` route must NOT — on the
        ASGI event loop a lock another UI worker or process holds freezes every concurrent SSE tick
        and poll on this worker until it is released. Async callers go through `_project_call_async`.
        """
        try:
            return fn()
        except ProjectConflictError as e:
            labels = {
                "project_id": "project assignment",
                "supertask_id": "super-task assignment",
                "label": "display name",
            }
            operations = {
                "project_id": "assign_project",
                "supertask_id": "assign_supertask",
                "label": "rename",
            }
            raise HTTPException(409, {
                "code": "run_organization_changed",
                "run_id": e.run_id,
                "operation": operations[e.field],
                "organization_field": e.field,
                "expected_value": e.expected,
                "current_value": e.current,
                f"expected_{e.field}": e.expected,
                f"current_{e.field}": e.current,
                "message": (
                    f"The run's {labels[e.field]} changed before this request could be applied; "
                    "no organization metadata was written."),
                "remediation": (
                    "Refresh the Runs list and repeat the change from the current run card."),
            }) from e
        except ProjectStoreLockError as e:
            raise HTTPException(503, str(e)) from e
        except ProjectError as e:
            raise HTTPException(400, str(e)) from e

    async def _project_call_async(fn):
        """`_project_call` off the event loop — the offload `assign_run` / `rename_run` already use."""
        return await anyio.to_thread.run_sync(lambda: _project_call(fn))

    def _expected_run_generation(body: dict) -> str:
        generation = body.get(EXPECTED_RUN_GENERATION_FIELD)
        if (not isinstance(generation, str)
                or _RUN_GENERATION_RE.fullmatch(generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": (
                    "expected_generation must be the exact generation from the Runs list."),
                "remediation": (
                    "Refresh the Runs list before changing this run's organization."),
            })
        return generation

    def _expected_organization_value(body: dict, field: str) -> str | None:
        expected_field = f"expected_{field}"
        if expected_field not in body:
            raise HTTPException(400, {
                "code": "invalid_expected_organization",
                "organization_field": field,
                "message": (
                    f"{expected_field} must be the exact value from the Runs list, including null."),
                "remediation": (
                    "Refresh the Runs list before changing this run's organization."),
            })
        value = body[expected_field]
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, {
                "code": "invalid_expected_organization",
                "organization_field": field,
                "message": f"{expected_field} must be a string or null.",
                "remediation": (
                    "Refresh the Runs list before changing this run's organization."),
            })
        return value

    def _run_project_call(
            run_id: str, expected_generation: str, operation: str, action: str, fn):
        """Fence run-id metadata against a reset/replacement generation under its sequencer."""
        rd = _run_dir(run_id)
        with srv.commands.sequence(rd):
            # Re-enter the canonical helper after waiting: deletion can publish its root fence or
            # quarantine the directory between the route's first lookup and sequencer acquisition.
            canonical = srv.commands.validate_paths(_run_dir(run_id))
            current_generation = srv.commands.run_generation(canonical)
            if current_generation != expected_generation:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "run_id": run_id,
                    "operation": operation,
                    "expected_generation": expected_generation,
                    "current_generation": current_generation or None,
                    "message": (
                        f"The run changed before {action}; "
                        "no organization metadata was written."),
                    "remediation": (
                        "Refresh the Runs list and repeat the change on the intended "
                        "current generation."),
                })
            return _project_call(fn)

    @router.get("/api/projects")
    def list_projects():
        return projects.load()

    @router.post("/api/projects")
    async def create_project(request: Request):
        body = await _json_object(request)
        p = await _project_call_async(
            lambda: projects.create(body.get("name", ""), body.get("parent_id")))
        return p.model_dump()

    @router.patch("/api/projects/{pid}")
    async def patch_project(pid: str, request: Request):
        body = await _json_object(request)

        def _apply():
            if "name" in body and body["name"] is not None:
                projects.rename(pid, body["name"])
            if "parent_id" in body:
                projects.reparent(pid, body["parent_id"])
        await _project_call_async(_apply)
        return {"ok": True}

    @router.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        _project_call(lambda: projects.delete(pid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/project")
    async def assign_run(run_id: str, request: Request):
        body = await _json_object(request)
        expected_generation = _expected_run_generation(body)
        expected_project_id = _expected_organization_value(body, "project_id")
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, expected_generation, "assign_project",
            "its project assignment could be updated",
            lambda: projects.assign(
                run_id, body.get("project_id"), expected_current=expected_project_id)))
        return {"ok": True}

    # ------------------------------------------------------------------ super-tasks (flat axis)
    @router.get("/api/supertasks")
    def list_supertasks():
        data = projects.load()
        return {"supertasks": data["supertasks"], "assignments": data["supertask_assignments"]}

    @router.post("/api/supertasks")
    async def create_supertask(request: Request):
        body = await _json_object(request)
        st = await _project_call_async(
            lambda: projects.create_supertask(body.get("name", ""), body.get("task_id")))
        return st

    @router.patch("/api/supertasks/{sid}")
    async def patch_supertask(sid: str, request: Request):
        body = await _json_object(request)
        await _project_call_async(lambda: projects.rename_supertask(sid, body.get("name", "")))
        return {"ok": True}

    @router.delete("/api/supertasks/{sid}")
    def delete_supertask(sid: str):
        _project_call(lambda: projects.delete_supertask(sid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/supertask")
    async def assign_supertask(run_id: str, request: Request):
        body = await _json_object(request)
        expected_generation = _expected_run_generation(body)
        expected_supertask_id = _expected_organization_value(body, "supertask_id")
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, expected_generation, "assign_supertask",
            "its super-task assignment could be updated",
            lambda: projects.assign_supertask(
                run_id, body.get("supertask_id"), expected_current=expected_supertask_id)))
        return {"ok": True}

    @router.patch("/api/runs/{run_id}")
    async def rename_run(run_id: str, request: Request):
        """Set/clear a run's UI display label. Non-destructive: the run dir id is unchanged."""
        body = await _json_object(request)
        expected_generation = _expected_run_generation(body)
        expected_label = _expected_organization_value(body, "label")
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, expected_generation, "rename", "it could be renamed",
            lambda: projects.set_label(
                run_id, body.get("label"), expected_current=expected_label)))
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
