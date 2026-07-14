"""Run-organization routes (ClearML-style projects, super-tasks, run labels, run delete). Handler
bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from looplab.serve.engine_proc import (
    _engine_alive, _engine_liveness, _fresh_resume_launch_pending, _fresh_run_launch_pending,
    _run_lifecycle_lock)
from looplab.serve.projects import ProjectError


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, projects = srv.run_dir, srv.projects

    # ------------------------------------------------------------------ projects (ClearML-style)
    def _project_call(fn):
        """Run one ProjectStore mutation, translating its ProjectError into the same 400 every
        projects/supertasks route raised inline (HTTPException(400, str(e)) — identical status and
        {"detail": ...} body, just written once)."""
        try:
            return fn()
        except ProjectError as e:
            raise HTTPException(400, str(e))

    @router.get("/api/projects")
    def list_projects():
        return projects.load()

    @router.post("/api/projects")
    async def create_project(request: Request):
        body = await request.json()
        p = _project_call(lambda: projects.create(body.get("name", ""), body.get("parent_id")))
        return p.model_dump()

    @router.patch("/api/projects/{pid}")
    async def patch_project(pid: str, request: Request):
        body = await request.json()

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
        _run_dir(run_id)   # 404 guard: only real runs can be filed
        body = await request.json()
        _project_call(lambda: projects.assign(run_id, body.get("project_id")))
        return {"ok": True}

    # ------------------------------------------------------------------ super-tasks (flat axis)
    @router.get("/api/supertasks")
    def list_supertasks():
        data = projects.load()
        return {"supertasks": data["supertasks"], "assignments": data["supertask_assignments"]}

    @router.post("/api/supertasks")
    async def create_supertask(request: Request):
        body = await request.json()
        st = _project_call(lambda: projects.create_supertask(body.get("name", ""), body.get("task_id")))
        return st

    @router.patch("/api/supertasks/{sid}")
    async def patch_supertask(sid: str, request: Request):
        body = await request.json()
        _project_call(lambda: projects.rename_supertask(sid, body.get("name", "")))
        return {"ok": True}

    @router.delete("/api/supertasks/{sid}")
    def delete_supertask(sid: str):
        _project_call(lambda: projects.delete_supertask(sid))
        return {"ok": True}

    @router.post("/api/runs/{run_id}/supertask")
    async def assign_supertask(run_id: str, request: Request):
        _run_dir(run_id)   # 404 guard: only real runs can be filed
        body = await request.json()
        _project_call(lambda: projects.assign_supertask(run_id, body.get("supertask_id")))
        return {"ok": True}

    @router.patch("/api/runs/{run_id}")
    async def rename_run(run_id: str, request: Request):
        """Set/clear a run's UI display label. Non-destructive: the run dir id is unchanged."""
        _run_dir(run_id)   # 404 guard
        body = await request.json()
        projects.set_label(run_id, body.get("label"))
        return {"ok": True}

    @router.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        """Permanently remove a run's directory and forget its UI metadata. Refuses ONLY while a LIVE
        engine still holds the run (its engine.lock) — so a finished run AND a stalled/zombie one (the
        engine died without emitting run_finished, so `finished` stays False) can both be deleted. The
        old guard keyed on `finished`, which wrongly blocked deleting a stalled run even though no
        engine was running. We still never yank the dir out from under a running engine.

        Liveness is the OS file lock (the SAME mechanism cli._engine_singleton uses to prevent two
        engines). If that ownership evidence is inaccessible or locking is unsupported, deletion
        fails closed; an explicit UI confirmation cannot make unknown writer ownership safe."""
        import shutil
        rd = _run_dir(run_id)
        with srv.commands.destructive_guard(rd, "delete run") as rd:
            # The command sequencer excludes command workers/current spawn leases. The lifecycle
            # fence additionally excludes the durable-resume reconciler and reset's pre-lock launch
            # window introduced by older/CLI-compatible control paths.
            with _run_lifecycle_lock(rd):
                liveness = _engine_liveness(rd)
                if liveness is None:
                    raise HTTPException(409, {
                        "code": "engine_liveness_unknown",
                        "message": "Run deletion was refused because engine ownership is unknown.",
                        "remediation": (
                            "Inspect engine.lock and storage locking, then retry only after the "
                            "engine is verifiably stopped."),
                        "retryable": True,
                    })
                # Keep the historical `_engine_alive` seam for downstream/tests while requiring the
                # authoritative tri-state probe to say exact False before destructive work.
                if (liveness is True or _engine_alive(rd) or _fresh_resume_launch_pending(rd)
                        or _fresh_run_launch_pending(rd)):
                    raise HTTPException(
                        409, "run is live or launching — pause/stop it before deleting")
                # A keyed Genesis start lives beside the sequencer so it survives response loss and a
                # partial run directory. Capture its exact identity while both lifecycle fences hold.
                start_record = srv.commands.load_start_record(rd)
                # The first guard flush handles live in-process ledgers before taking the sequencer.
                # This durable-only pass closes the fresh-AppState/crashed-process window.
                flush_durable_costs = getattr(srv, "flush_durable_run_costs", None)
                if not callable(flush_durable_costs):
                    raise HTTPException(
                        503, "cannot delete run: durable run-cost recovery is unavailable")
                try:
                    durable_costs_flushed = flush_durable_costs(rd)
                except Exception as exc:  # noqa: BLE001 - fail closed on unknown evidence
                    raise HTTPException(
                        503, "cannot delete run: durable run-cost recovery failed") from exc
                if durable_costs_flushed is not True:
                    raise HTTPException(
                        409, "cannot delete run: run-cost evidence is pending, busy, malformed, or conflicting")

                start_record_retired = False
                if start_record is not None:
                    if not srv.commands.retire_start_record(rd, str(start_record["id"])):
                        raise HTTPException(
                            503, "run start record could not be retired; the run was not deleted")
                    start_record_retired = True
                # Retry once for transient FUSE visibility; report success only after the directory
                # is really gone. Restore the exact start record if deletion was partial.
                for _ in range(2):
                    shutil.rmtree(rd, ignore_errors=True)
                    if not rd.exists():
                        break
                if rd.exists():
                    if start_record_retired:
                        try:
                            srv.commands.save_start_record(rd, start_record)
                        except Exception as exc:  # noqa: BLE001 - durable ownership loss must be loud
                            raise HTTPException(
                                503, "run deletion failed and its start record could not be restored") from exc
                    leftover = next((str(p.relative_to(rd)) for p in rd.rglob("*")), "(dir)")
                    raise HTTPException(
                        500, f"run dir could not be fully removed (e.g. {leftover!r} — a file "
                             "may be open or the storage is read-only); retry once nothing holds it")
                projects.forget(run_id)
                srv.summary_cache.pop(run_id, None)
        return {"ok": True}

    return router
