"""Run-organization routes (ClearML-style projects, super-tasks, run labels, run delete). Handler
bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import re

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from looplab.serve.deletion_service import (
    begin_or_resume_run_deletion, deletion_cascade_requested, get_run_deletion,
    validate_deletion_request)
from looplab.serve.memory_cascade import (MEMORY_CASCADE_SCHEMA, attributable_memory,
                                          known_memory_dirs, memory_dir_is_known,
                                          purge_attributable_memory, run_memory_identity)
from looplab.serve.http import json_object
from looplab.serve.projects import ProjectConflictError, ProjectError, ProjectStoreLockError
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD


_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, projects = srv.run_dir, srv.projects

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
        body = await json_object(request)
        p = await _project_call_async(
            lambda: projects.create(body.get("name", ""), body.get("parent_id")))
        return p.model_dump()

    @router.patch("/api/projects/{pid}")
    async def patch_project(pid: str, request: Request):
        body = await json_object(request)

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
        body = await json_object(request)
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
        body = await json_object(request)
        st = await _project_call_async(
            lambda: projects.create_supertask(body.get("name", ""), body.get("task_id")))
        return st

    @router.patch("/api/supertasks/{sid}")
    async def patch_supertask(sid: str, request: Request):
        body = await json_object(request)
        await _project_call_async(lambda: projects.rename_supertask(sid, body.get("name", "")))
        return {"ok": True}

    @router.delete("/api/supertasks/{sid}")
    def delete_supertask(sid: str):
        _project_call(lambda: projects.delete_supertask(sid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/supertask")
    async def assign_supertask(run_id: str, request: Request):
        body = await json_object(request)
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
        body = await json_object(request)
        expected_generation = _expected_run_generation(body)
        expected_label = _expected_organization_value(body, "label")
        await anyio.to_thread.run_sync(lambda: _run_project_call(
            run_id, expected_generation, "rename", "it could be renamed",
            lambda: projects.set_label(
                run_id, body.get("label"), expected_current=expected_label)))
        return {"ok": True}

    def _memory_dir() -> str:
        return getattr(srv.global_settings(), "memory_dir", "") or ""

    def _identity(run_id: str) -> dict:
        """The run's OWN memory identity, read from the run directory while it still exists.

        Both halves matter and neither is available afterwards. `run_uid` is what distinguishes this
        incarnation from the next run to reuse the directory name — matching on the name alone
        deleted a different, still-existing run's rows. `memory_dir` is a per-RUN setting, so the
        server's current global can be a store this run never wrote to.
        """
        return run_memory_identity(_plain_run_dir(run_id), fallback_memory_dir=_memory_dir())

    def _plain_run_dir(run_id: str):
        """The run directory, through the SAME containment guard the deletion transaction uses —
        reserved names, traversal, control characters and Windows device names all refused there,
        and a second spelling of that guard is a second thing to get wrong."""
        from looplab.serve.deletion_service import _plain_run_path
        return _plain_run_path(srv, run_id)

    @router.get("/api/runs/{run_id}/memory-attribution")
    async def run_memory_attribution(run_id: str):
        """What a cascading delete WOULD remove from cross-run memory, and what it would keep.

        Read-only, and deliberately a separate route from the deletion itself: the operator has to
        be able to see the answer BEFORE agreeing to it, and a number that only appears in a receipt
        is a number nobody consented to.
        """
        def _survey():
            ident = _identity(run_id)
            return {**attributable_memory(ident["memory_dir"], ident["run_id"], ident["run_uid"]),
                    "memory_dir": ident["memory_dir"]}
        return await anyio.to_thread.run_sync(_survey)

    @router.post("/api/runs/{run_id}/memory-purge")
    async def run_memory_purge(run_id: str, request: Request):
        """Finish a cascade whose store was locked at the moment the run was deleted.

        It takes the IDENTITY in the body — `run_uid` and `memory_dir` as returned by the deletion
        receipt — because by the time this is worth calling the run directory is gone and neither can
        be recovered from the run id alone. That is also why it is not "keyed by run id and needs no
        fence": a bare run id names a directory NAME, which the next run reuses, and this endpoint
        irreversibly rewrites five shared stores. It refuses without an identity rather than guessing
        one, and when the run still exists it re-reads the identity from the run and requires the
        body to agree.
        """
        body = await json_object(request)
        extra = set(body) - {"run_uid", "memory_dir"}
        if extra:
            raise HTTPException(400, {"code": "invalid_memory_purge_request",
                                      "message": "memory-purge accepts only run_uid and memory_dir."})
        run_uid = str(body.get("run_uid") or "")
        memory_dir = str(body.get("memory_dir") or "")

        def _purge():
            ident = _identity(run_id)
            live = _plain_run_dir(run_id).is_dir()
            if live:
                # The run is still here, so its own record outranks the body — and a body that
                # disagrees is a client acting on a stale identity, which is the case that would
                # purge the wrong incarnation.
                if run_uid and ident["run_uid"] and run_uid != ident["run_uid"]:
                    raise HTTPException(409, {
                        "code": "memory_purge_identity_changed",
                        "message": "This run id now names a different run incarnation."})
                return purge_attributable_memory(
                    ident["memory_dir"], ident["run_id"], ident["run_uid"])
            if not run_uid and not memory_dir:
                raise HTTPException(400, {
                    "code": "memory_purge_identity_required",
                    "message": ("This run no longer exists, so its identity cannot be read back. "
                                "Pass the run_uid and memory_dir from the deletion receipt.")})
            # THE STORE IS NOT THE CALLER'S TO CHOOSE. Everything else on this router routes a path
            # through `_plain_run_dir`'s containment guard; this one arrives from the request body and
            # goes straight to a destructive rewrite — `purge_attributable_memory` checks only
            # `base.is_dir()` and then rewrites five `.jsonl` files under it, and with no `run_uid`
            # the ownership test degrades to matching a row's bare `run_id`, which a reused directory
            # name satisfies. Containment would not have helped: an absolute path to somebody else's
            # store contains no traversal. So the check is membership in what this SERVER can name,
            # which is also why the refusal is separate from the identity one above — "I do not know
            # that store" and "you told me nothing" are different answers.
            store = memory_dir or _memory_dir()
            if not memory_dir_is_known(store, known_memory_dirs(
                    srv.root, fallback_memory_dir=_memory_dir())):
                raise HTTPException(400, {
                    "code": "memory_purge_store_unknown",
                    "message": ("That memory_dir is not a cross-run store this server manages. "
                                "Pass the memory_dir from this run's deletion receipt.")})
            return purge_attributable_memory(store, run_id, run_uid)

        return await anyio.to_thread.run_sync(_purge)

    @router.post("/api/runs/{run_id}/deletions")
    async def create_run_deletion(run_id: str, request: Request):
        """Delete one exact run generation through an operation-bound durable transaction."""
        body = await json_object(request)
        operation_id, generation, expected_seq = validate_deletion_request(body)
        cascade = deletion_cascade_requested(body)
        def _identity_before_deletion() -> dict:
            # Record whether the run was still HERE when the identity was read, not just what the
            # read returned. On a RETRY of an already-succeeded deletion the directory is gone (the
            # receipt outlives it in `srv.root`), `run_memory_identity` swallows the FileNotFoundError
            # and falls back to the directory NAME plus the SERVER's current global store — so the
            # purge below would run under an identity this run never had, against a store it may
            # never have written to, and report a clean success. Guessing an identity is the one
            # thing this cascade exists not to do.
            #
            # REVIEW (mega-review 2026-08-13): the identity read here is NEVER persisted — the
            # durable deletion receipt is a closed key set (`deletion_transaction._RECEIPT_KEYS`,
            # loader refuses extras) with no run_uid/memory_dir, and on a 202 this request's local
            # is simply discarded. So any deletion that COMPLETES on a retry after the directory is
            # gone (the four documented "left the workspace but pending" phases, or a crash
            # mid-transaction) loses the uid permanently, and the failure block below then tells
            # the operator to pass "the run_uid and memory_dir from the original deletion receipt"
            # — fields no receipt or 202 body ever contained. The only survivor is the volatile UI
            # notice from a succeeded-with-partial-purge response (same page session only). The
            # identity read on the FIRST attempt should be stored beside the receipt (a sidecar,
            # since the receipt schema is deliberately closed) so the retry can finish the cascade
            # uid-keyed instead of degrading to the bare-name matching 61021d2 eliminated.
            return {**_identity(run_id), "read_from_run": _plain_run_dir(run_id).is_dir()}

        identity = (await anyio.to_thread.run_sync(_identity_before_deletion)) if cascade else {}
        result = await anyio.to_thread.run_sync(lambda: begin_or_resume_run_deletion(
            srv, run_id, operation_id=operation_id,
            expected_generation=generation, expected_seq=expected_seq))
        # ORDER IS THE POINT: the cross-run purge runs only once the run itself is durably gone. A
        # deletion that fails after its memory was purged would leave the operator with a run whose
        # evidence had been destroyed on its behalf — the one outcome no retry can undo. The purge is
        # idempotent ("remove every row attributable solely to R"), so running it on a retry of an
        # already-succeeded operation costs nothing and repairs a partial first attempt — but only
        # when THIS attempt could still read the identity to repair it under.
        if cascade and result.get("status") == "succeeded":
            # Read BEFORE the deletion, used after: `run_uid` and the run's own `memory_dir` live in
            # the run directory, which no longer exists by the time the purge runs.
            if not identity.get("read_from_run"):
                result = {**result, "memory": {
                    "schema": MEMORY_CASCADE_SCHEMA, "run_id": run_id, "run_uid": "",
                    "memory_dir": "", "identity": "unknown", "ok": False,
                    "deleted": 0, "kept": 0, "stores": [], "failures": [{
                        "store": "identity",
                        "error": ("the run directory was already gone when this attempt read its "
                                  "identity, so the purge would have guessed one; POST "
                                  f"/api/runs/{run_id}/memory-purge with the run_uid and memory_dir "
                                  "from the original deletion receipt")}]}}
            else:
                result = {**result, "memory": await anyio.to_thread.run_sync(
                    lambda: purge_attributable_memory(
                        identity["memory_dir"], identity["run_id"], identity["run_uid"]))}
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
