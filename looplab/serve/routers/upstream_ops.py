"""Assistant/external-agent upstream APIs over the shared engine transaction."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from looplab.adapters.tasks import load_task
from looplab.core.config import read_config_snapshot
from looplab.core.errors import ConfigRefusal, UpstreamRefusal
from looplab.engine.upstream import UpstreamLane
from looplab.events.eventstore import EventStoreConcurrencyError, EventStoreLockError
from looplab.serve.principal import request_agent_token


class UpstreamAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class UpstreamProposal(UpstreamAction):
    source_node_id: int = Field(ge=0, strict=True)
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    hunk_hashes: list[str] = Field(min_length=1, max_length=128)
    files: dict[str, str]
    deleted: list[str] = Field(default_factory=list, max_length=128)
    recipe_files: dict[str, str]
    recipe_deleted: list[str] = Field(default_factory=list, max_length=128)
    summary: str = Field(min_length=1, max_length=700)
    flag: dict[str, str]
    documentation_path: str = Field(min_length=1, max_length=1024)
    critic: dict[str, str]


class UpstreamCheck(UpstreamAction):
    proposal_id: str = Field(pattern=r"^up_[0-9a-f]{24}$")


class UpstreamAdvance(UpstreamCheck):
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodeWorktree(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    start: str = Field("base", max_length=80)


class CodeOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worktree: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    patch: Optional[str] = Field(None, max_length=2 * 1024 * 1024)
    start: str = Field("base", max_length=80)


class UpstreamRecovery(UpstreamAction):
    claim_action_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=700)


def build_router(srv):
    router = APIRouter()

    def lane(run_id):
        rd = srv.run_dir(run_id)
        task = load_task(rd / "task.snapshot.json", existing_run=True)
        if not hasattr(task, "seed_base"):
            raise HTTPException(409, {"code": "upstream_disabled", "message": "Upstream requires a pinned repo task"})
        return UpstreamLane(rd, task, read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=True))

    def call(run_id, operation, *args, credential=None, **kwargs):
        try:
            current = lane(run_id)
            if credential is not None and request_agent_token(credential) and not current.settings.external_harness:
                raise HTTPException(403, {"code": "agent_token_refused", "message": "Upstream writes on internal runs require the operator credential"})
            return getattr(current, operation)(*args, **kwargs)
        except UpstreamRefusal as exc:
            status = 503 if "unavailable" in exc.code else 409
            raise HTTPException(status, {"code": exc.code, "message": str(exc)}) from exc
        except (ConfigRefusal, OSError, ValueError) as exc:
            raise HTTPException(503, {"code": "upstream_source_unavailable", "message": "Read and restore the run task/config/events/archive source before acting"}) from exc
        except (EventStoreConcurrencyError, EventStoreLockError) as exc:
            raise HTTPException(503, {"code": "upstream_publication_unavailable", "message": "Read upstream history before explicit exact recovery; no publication verdict is available"}) from exc

    @router.get("/api/runs/{run_id}/upstream")
    def status(run_id: str, response: Response, expected_generation: str = Query(..., pattern=r"^[0-9a-f]{64}$"),
               offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100),
               source_node_id: Optional[int] = Query(None, ge=0),
               candidate_offset: int = Query(0, ge=0), candidate_limit: int = Query(200, ge=1, le=200)):
        """Read current base, independently paged nominations/history, optionally for one source. Starts no work."""
        response.headers["Cache-Control"] = "no-store"
        return call(run_id, "read", expected_generation, offset=offset, limit=limit,
                    source_node_id=source_node_id, candidate_offset=candidate_offset, candidate_limit=candidate_limit)

    @router.get("/api/runs/{run_id}/upstream/requests/{proposal_id}")
    def proposal_request(run_id: str, proposal_id: str, response: Response,
                         expected_generation: str = Query(..., pattern=r"^[0-9a-f]{64}$"),
                         expected_request_hash: str = Query(..., pattern=r"^[0-9a-f]{64}$"),
                         offset: int = Query(0, ge=0, lt=2 * 1024 * 1024),
                         limit: int = Query(2048, ge=1, le=4096),
                         expected_content_hash: Optional[str] = Query(None, pattern=r"^[0-9a-f]{64}$")):
        """Read one fenced base64 page of the original proposal. Diagnostic only; starts no work.

        Preserve content_sha256 for subsequent pages, assemble all bytes and verify
        both content and canonical request hashes before considering exact recovery.
        """
        response.headers["Cache-Control"] = "no-store"
        return call(run_id, "request", expected_generation, proposal_id, expected_request_hash,
                    offset=offset, limit=limit, expected_content_hash=expected_content_hash)

    @router.post("/api/runs/{run_id}/upstream/proposals")
    def propose(run_id: str, body: UpstreamProposal, request: Request):
        """Author a generalized capability in a run-owned Maintainer git worktree; no base switch."""
        return call(run_id, "propose", body.model_dump(), credential=request)

    @router.post("/api/runs/{run_id}/upstream/check")
    def check(run_id: str, body: UpstreamCheck, request: Request):
        """Buy explicit real source repetitions, old-recipe artifact regressions and trigger tests.

        Can outlive a lost HTTP response. Read upstream history and retry exactly;
        an unresolved claim refuses re-execution. No model verdict certifies a pass.
        """
        return call(run_id, "check", body.model_dump(), credential=request)

    @router.post("/api/runs/{run_id}/upstream/advance")
    def advance(run_id: str, body: UpstreamAdvance, request: Request):
        """Explicit current-evidence CAS, only with a stopped engine; then resume separately."""
        return call(run_id, "advance", body.model_dump(), credential=request)

    @router.post("/api/runs/{run_id}/upstream/recover")
    def recover(run_id: str, body: UpstreamRecovery, request: Request):
        """Operator abandonment of an interrupted claim, granting no pass and no resume."""
        if request_agent_token(request):
            raise HTTPException(403, {"code": "operator_required", "message": "Only the operator can abandon an interrupted claim"})
        return call(run_id, "abandon", body.model_dump())

    # ------------------------------------------------------------- doc 79: the run's code repository

    def code_call(run_id, fn, *, request=None, write=False):
        from looplab.engine.code_repo import CodeRepoError
        rd = srv.run_dir(run_id)
        if not (rd / "events.jsonl").is_file():
            raise HTTPException(404, {"code": "run_not_found", "message": f"no run {run_id}"})
        if write and request is not None and request_agent_token(request):
            try:
                settings = read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=False)
            except (ConfigRefusal, OSError, ValueError):
                settings = None
            if not getattr(settings, "external_harness", False):
                raise HTTPException(403, {"code": "agent_token_refused", "message":
                                          "Code writes on internal runs require the operator credential"})
        try:
            return fn(rd)
        except CodeRepoError as exc:
            status = 503 if exc.code.startswith("git_") else (409 if exc.code.endswith("_exists") else 400)
            raise HTTPException(status, {"code": exc.code, "message": str(exc)}) from exc
        except OSError as exc:
            raise HTTPException(503, {"code": "code_storage_unavailable",
                                      "message": "The run's code repository could not be read"}) from exc

    @router.get("/api/runs/{run_id}/code")
    def code_status(run_id: str, response: Response):
        """The run's code: bases (with upstream evidence), per-attempt commits with changed paths,
        metric and effective config, what is pending upstream, and the branches. Starts no work."""
        response.headers["Cache-Control"] = "no-store"

        def read(rd):
            from looplab.engine.code_identity import code_status as status_of
            try:
                task = load_task(rd / "task.snapshot.json", existing_run=True)
            except (ConfigRefusal, OSError, ValueError):
                task = None
            try:
                settings = read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=False)
            except (ConfigRefusal, OSError, ValueError):
                settings = None
            return status_of(rd, task=task, settings=settings)
        return code_call(run_id, read)

    @router.get("/api/runs/{run_id}/code/diff")
    def code_diff(run_id: str, response: Response,
                  commit: str = Query(..., pattern=r"^[0-9a-f]{7,64}$"),
                  against: Optional[str] = Query(None, pattern=r"^[0-9a-f]{7,64}$"),
                  max_bytes: int = Query(256 * 1024, ge=1024, le=2 * 1024 * 1024)):
        """A bounded unified diff of one commit of the run's code repository against `against`
        (default: its parent, i.e. the base it was seeded on). Read-only."""
        response.headers["Cache-Control"] = "no-store"

        def read(rd):
            from looplab.engine.code_repo import CodeRepo
            repo = CodeRepo(rd)
            sha = repo.rev(commit) if repo.exists() else None
            if sha is None:
                raise HTTPException(404, {"code": "code_commit_unknown", "message": "no such commit"})
            old = repo.rev(against) if against else repo.rev(sha + "^")
            if old is None:
                raise HTTPException(404, {"code": "code_commit_unknown", "message": "no comparison commit"})
            text, truncated = repo.diff(old, sha, max_bytes=max_bytes)
            return {"commit": sha, "against": old, "changed": repo.changed(old, sha),
                    "diff": text, "truncated": truncated}
        return code_call(run_id, read)

    @router.post("/api/runs/{run_id}/code/sync")
    def code_sync(run_id: str, request: Request):
        """Build the attempt commits the log implies but no code_snapshot recorded (old runs).
        Writes only the run's private code repository; appends no event."""
        def write(rd):
            from looplab.engine.code_identity import sync_run
            out = sync_run(rd)
            return {"built": len(out["built"]), "recorded": out["recorded"],
                    "unavailable": [b for b in out["built"] if b.get("status") != "recorded"][:20]}
        return code_call(run_id, write, request=request, write=True)

    @router.post("/api/runs/{run_id}/code/worktrees")
    def code_worktree(run_id: str, body: CodeWorktree, request: Request):
        """Check out the run's active base (or `node:<id>`) from the PRIVATE run repository into
        `RUN_DIR/code/worktrees/<name>` on branch `agent/<name>`, for an external agent to edit.
        Submit it with `inject_node` `code_from: {"worktree": name}`."""
        def write(rd):
            from looplab.engine.code_worktree import add_worktree
            return add_worktree(rd, body.name, start=body.start)
        return code_call(run_id, write, request=request, write=True)

    @router.post("/api/runs/{run_id}/code/overlay")
    def code_overlay(run_id: str, body: CodeOverlay, request: Request):
        """Preview the `files`/`deleted` overlay a worktree or a patch becomes (the same conversion
        `inject_node` `code_from` applies), held to the edit surface. Submits nothing."""
        if (body.worktree is None) == (body.patch is None):
            raise HTTPException(400, {"code": "code_from_invalid",
                                      "message": "give exactly one of worktree or patch"})

        def compute(rd):
            from looplab.engine.code_worktree import overlay_from_patch, overlay_from_worktree
            if body.worktree is not None:
                return overlay_from_worktree(rd, body.worktree)
            return overlay_from_patch(rd, body.patch, start=body.start)
        return code_call(run_id, compute, request=request, write=True)

    return router
