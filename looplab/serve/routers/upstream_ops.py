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


class UpstreamRebase(BaseModel):
    """A source measured on an OLDER base, carried with its overlay three-way merged onto the current
    one (doc 73 §4.3); the lane recomputes the merge and refuses one that differs
    (`UpstreamLane._rebased_source`). Bounds as `engine/upstream_spec.py::normalize_request`."""
    model_config = ConfigDict(extra="forbid")
    from_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, str] = Field(max_length=128)
    deleted: list[str] = Field(max_length=128)


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
    # OPTIONAL, and ABSENT from the lane's body when not given (`propose` dumps without it): every
    # body written before it normalizes, digests and retries exactly as it did. It was missing here
    # while the lane accepted it, so a rebased proposal over HTTP was a 422 (review 2026-10-15).
    rebase: Optional[UpstreamRebase] = None


class UpstreamCheck(UpstreamAction):
    proposal_id: str = Field(pattern=r"^up_[0-9a-f]{24}$")


class UpstreamAdvance(UpstreamCheck):
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_token: str = Field(pattern=r"^[0-9a-f]{64}$")


_GENERATION = r"^[0-9a-fA-F]{64}$"


class CodeWorktree(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    start: str = Field("base", max_length=80)
    # OPTIONAL (2026-10-16): an absolute directory OUTSIDE the run directory and the operator's
    # source tree, for a run root on tmpfs (`engine/code_worktree.py::_worktree_location`).
    path: Optional[str] = Field(None, min_length=1, max_length=4096)
    # OPTIONAL generation fence (the MCP tools always send it): a worktree of a REPLACED run's
    # repository is refused instead of handed to an agent that believes it is the current one.
    # Either case, compared lowercased: the MCP client accepts an uppercase token from /state.
    expected_generation: Optional[str] = Field(None, pattern=_GENERATION)


class CodeFromGit(BaseModel):
    """A foreign local repository's branch or commit (`engine/code_foreign.py`)."""
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=1, max_length=4096)
    ref: str = Field(min_length=1, max_length=256)
    prefix: Optional[str] = Field(None, max_length=1024)


class CodeOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worktree: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    patch: Optional[str] = Field(None, max_length=2 * 1024 * 1024)
    git: Optional[CodeFromGit] = None
    # A patch's only (`serve/protocol.py::code_from_shape_error`); absent means "base".
    start: Optional[str] = Field(None, max_length=80)
    expected_generation: Optional[str] = Field(None, pattern=_GENERATION)
    # False = paths and sizes only (`code_worktree.py::overlay_summary`): what an MCP client reads,
    # whose reply cap a whole overlay can exceed.
    include_files: bool = True


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
        return call(run_id, "propose",
                    body.model_dump(exclude={"rebase"} if body.rebase is None else None),
                    credential=request)

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

    def generation_moved(expected, current):
        from looplab.serve.http import generation_conflict
        return generation_conflict("the run was replaced since expected_generation was read",
                                   expected=expected, current=current,
                                   remediation="read /state and retry with its generation")

    def code_call(run_id, fn, *, request=None, write=False, expected_generation=None,
                  sequenced=False, prepare=None):
        """One code route's fence, credential and error mapping (`serve/code_from.py`).

        THE GENERATION FENCE (review 2026-10-16). A read — and the overlay PREVIEW, which writes
        only run-private git objects and can fetch for minutes — is a compare-and-swap ACROSS the
        work through `commands.generation_fence` (taken before AND after; never the exclusive
        sequencer). A `sequenced` write — creating a worktree — compares under
        `commands.sequence`, so a run replaced between the check and the write cannot be handed a
        worktree of its predecessor's repository; its long half (`prepare`, the idempotent
        `sync_run`) runs before the lock is taken. `fn` receives the run dir and the generation the
        fence observed."""
        from looplab.engine.code_repo import CodeRepoError
        from looplab.serve.code_from import code_http_error
        rd = srv.run_dir(run_id)
        if not (rd / "events.jsonl").is_file():
            raise HTTPException(404, {"code": "run_not_found", "message": f"no run {run_id}"})
        expected = expected_generation.lower() if expected_generation is not None else None
        if write and request is not None and request_agent_token(request):
            try:
                settings = read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=False)
            except (ConfigRefusal, OSError, ValueError):
                settings = None
            if not getattr(settings, "external_harness", False):
                raise HTTPException(403, {"code": "agent_token_refused", "message":
                                          "Code writes on internal runs require the operator credential"})
        try:
            if sequenced:
                if prepare is not None:
                    prepare(rd)
                with srv.commands.sequence(rd):
                    # Writer side: an unreadable log is "", which never matches.
                    current = srv.commands.run_generation(rd)
                    if expected is not None and current != expected:
                        raise generation_moved(expected, current)
                    return fn(rd, current)
            rd, before = srv.commands.generation_fence(rd)
            if expected is not None and before != expected:
                raise generation_moved(expected, before)
            out = fn(rd, before)
            if expected is not None:
                after = srv.commands.generation_fence(rd)[1]
                if after != before:
                    raise generation_moved(expected, after)
            return out
        except (CodeRepoError, OSError) as exc:
            raise code_http_error(exc) from exc

    @router.get("/api/runs/{run_id}/code")
    def code_status(run_id: str, response: Response):
        """The run's code: bases (with upstream evidence), per-attempt commits with changed paths,
        metric and effective config, what is pending upstream, and the branches. Starts no work."""
        response.headers["Cache-Control"] = "no-store"

        def read(rd, _generation):
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

        def read(rd, _generation):
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
        def write(rd, _generation):
            from looplab.engine.code_identity import sync_run
            out = sync_run(rd)
            return {"built": len(out["built"]), "recorded": out["recorded"],
                    "unavailable": [b for b in out["built"] if b.get("status") != "recorded"][:20]}
        return code_call(run_id, write, request=request, write=True)

    @router.post("/api/runs/{run_id}/code/worktrees")
    def code_worktree(run_id: str, body: CodeWorktree, request: Request):
        """Check out the run's active base (or `node:<id>`) from the PRIVATE run repository into
        `RUN_DIR/code/worktrees/<name>` (or `path`, outside the run directory and the operator's
        source tree) on branch `agent/<name>`, for an external agent to edit. `git merge
        nodes/n<id>` there combines another experiment's change. Submit it with `inject_node`
        `code_from: {"worktree": name}`; the reply's `guide` names each step."""
        def write(rd, _generation):
            from looplab.engine.code_worktree import add_worktree, worktree_guide
            meta = add_worktree(rd, body.name, start=body.start, path=body.path)
            return {**meta, "guide": worktree_guide(meta)}

        def prepare(rd):
            from looplab.engine.code_identity import sync_run
            sync_run(rd)                     # the long half, before the sequencer is taken
        return code_call(run_id, write, request=request, write=True,
                         expected_generation=body.expected_generation, sequenced=True,
                         prepare=prepare)

    @router.get("/api/runs/{run_id}/code/worktrees")
    def code_worktrees(run_id: str, response: Response,
                       expected_generation: Optional[str] = Query(None, pattern=_GENERATION)):
        """The run's agent worktrees: path (in or outside the run directory), branch, start, HEAD
        now, and whether each still starts on the active base. Read-only; starts no work."""
        response.headers["Cache-Control"] = "no-store"

        def read(rd, generation):
            from looplab.engine.code_worktree import describe_worktrees
            # The generation the fence held across this read, not a third, separate reading.
            return {"generation": generation, "worktrees": describe_worktrees(rd)}
        return code_call(run_id, read, expected_generation=expected_generation)

    @router.post("/api/runs/{run_id}/code/overlay")
    def code_overlay(run_id: str, body: CodeOverlay, request: Request):
        """Preview the `files`/`deleted` overlay a worktree, a patch or a foreign git branch
        (`git: {repo, ref}`, three-way merged onto the run's base; a conflict is 409 with its paths)
        becomes — the same conversion `inject_node` `code_from` applies — held to the edit surface.
        `include_files=false` returns paths and sizes only. Submits nothing, and is refused exactly
        as the inject would be (`serve/code_from.py::compute_overlay`): an empty change, a stale
        base (409 with its paths), a missing worktree directory; the agent token may name only the
        run's own source repository (or a worktree of it) as `git.repo`."""
        spec = {name: value for name, value in (
            ("worktree", body.worktree), ("patch", body.patch),
            ("git", body.git.model_dump(exclude_none=True) if body.git is not None else None),
            ("start", body.start)) if value is not None}

        def compute(rd, _generation):
            from looplab.serve.code_from import compute_overlay
            return compute_overlay(rd, spec, agent_token=bool(request_agent_token(request)),
                                   include_files=body.include_files)
        return code_call(run_id, compute, request=request, write=True,
                         expected_generation=body.expected_generation)

    return router
