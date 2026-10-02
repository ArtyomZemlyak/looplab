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
               offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100)):
        """Read current base, per-hunk nominations and paged measured upstream history. Starts no work."""
        response.headers["Cache-Control"] = "no-store"
        return call(run_id, "read", expected_generation, offset=offset, limit=limit)

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

    return router
