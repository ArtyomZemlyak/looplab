"""Campaign routes (doc 81): the in-run protocol journal, per-query uploads, LoopLab-computed
paired comparisons, shared-test leaderboards and the super-task campaign report.

Every write is idempotent on an `action_id` and stamps its AUTHOR from the credential (the scoped
agent token or the owner); every computed table names `computed_by`. The bodies live in
`harness/campaign.py`, `harness/per_query.py` and `harness/campaign_report.py`; this module is the
HTTP shape only. GETs read sidecars lock-free (append-only JSONL) and never wait on the sequencer.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

import anyio
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from looplab.serve.principal import request_agent_token


class AmendmentBody(BaseModel):
    """One protocol amendment: a pre-registered experiment, a task/file change or a budget."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_generation: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    action_id: str = Field(min_length=1, max_length=160)
    kind: Literal["experiment", "task", "files", "budget"]
    reason: str = Field(min_length=8, max_length=2000)
    experiment: Optional[dict[str, Any]] = None
    task: Optional[dict[str, Any]] = None
    files: Optional[list[dict[str, Any]]] = Field(default=None, max_length=16)
    budget: Optional[dict[str, Any]] = None

    def request_payload(self) -> dict:
        payload = self.model_dump(exclude={"expected_generation"})
        given = {k for k in ("experiment", "task", "files", "budget") if payload.get(k) is not None}
        if given != {self.kind}:
            raise HTTPException(400, f"a {self.kind} amendment carries exactly the {self.kind!r} field")
        return payload


class PerQueryUploadBody(BaseModel):
    """A terminal node's per-query evaluation arrays on one shared test set."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_generation: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    action_id: str = Field(min_length=1, max_length=160)
    node_id: int = Field(ge=0)
    format: Literal["npz", "json"] = "npz"
    content_b64: Optional[str] = None
    workdir_path: Optional[str] = Field(default=None, max_length=1024)
    query_key: str = Field(default="keys", min_length=1, max_length=128)
    cluster_key: Optional[str] = Field(default="users", max_length=128)
    metrics: dict[str, dict[str, Any]]
    test_label: Optional[str] = Field(default=None, max_length=64)
    experiment_id: Optional[str] = Field(default=None, max_length=64)
    arm: Optional[str] = Field(default=None, max_length=64)
    measured_check: Optional[dict[str, Any]] = None

    def request_payload(self, source_sha256: str) -> dict:
        payload = self.model_dump(exclude={"expected_generation", "content_b64"})
        payload["source_sha256"] = source_sha256
        return payload


class NarrativeBody(BaseModel):
    """The agent- or operator-authored narrative of a super-task's campaign report."""

    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    title: Optional[str] = Field(default=None, max_length=300)
    body_markdown: str = Field(min_length=1)
    key_findings: list[str] = Field(default_factory=list, max_length=20)


class PolicyBody(BaseModel):
    """The operator's limits on agent amendments for one run (owner token only)."""

    model_config = ConfigDict(extra="forbid")
    amendments: Literal["off", "experiments", "inputs", "all"] = "all"
    max_agent_added_nodes: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    note: Optional[str] = Field(default=None, max_length=1000)


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir = srv.run_dir

    @router.get("/api/runs/{run_id}/protocol")
    def get_protocol(run_id: str):
        """The run's pre-registered experiments, its amendment audit trail and the agent policy."""
        from looplab.harness.campaign import protocol_view
        return protocol_view(_run_dir(run_id))

    @router.post("/api/runs/{run_id}/protocol/amendments")
    async def post_amendment(run_id: str, body: AmendmentBody, request: Request):
        """Append an experiment, or record a task/file/budget amendment with its reason and diff."""
        from looplab.harness.campaign import publish_amendment
        agent = request_agent_token(request)
        body.request_payload()          # the kind/field pairing is a 400 before any lock
        return await anyio.to_thread.run_sync(
            lambda: publish_amendment(srv, _run_dir(run_id), body, agent_token=agent))

    @router.get("/api/runs/{run_id}/harness-policy")
    def get_harness_policy(run_id: str):
        """What the operator lets the agent amend in this run, and its node-addition cap."""
        from looplab.harness.campaign import agent_added_nodes, read_policy
        rd = _run_dir(run_id)
        return {**read_policy(rd), "agent_added_nodes": agent_added_nodes(srv, rd)}

    @router.put("/api/runs/{run_id}/harness-policy")
    def put_harness_policy(run_id: str, body: PolicyBody):
        """Set the agent's amendment level and node cap (the agent token is refused here)."""
        from looplab.harness.campaign import write_policy
        return write_policy(_run_dir(run_id), body.model_dump())

    @router.post("/api/runs/{run_id}/per-query")
    async def post_per_query(run_id: str, body: PerQueryUploadBody, request: Request):
        """Upload a terminal node's per-query arrays; LoopLab derives the shared test id."""
        from looplab.harness.per_query import publish_upload
        agent = request_agent_token(request)
        return await anyio.to_thread.run_sync(
            lambda: publish_upload(srv, _run_dir(run_id), body, agent_token=agent))

    @router.get("/api/runs/{run_id}/per-query")
    def get_per_query(run_id: str):
        """The run's current per-query uploads (latest per node and test) and their test ids."""
        from looplab.harness.per_query import run_per_query_view
        return run_per_query_view(_run_dir(run_id))

    @router.get("/api/runs/{run_id}/experiments/{experiment_id}/comparison")
    def get_comparison(run_id: str, experiment_id: str,
                       bootstrap: Optional[int] = Query(default=None, ge=100, le=100_000),
                       seed: int = Query(default=0, ge=0, le=2**31 - 1)):
        """The experiment's declared contrasts, computed now by LoopLab with provenance."""
        from looplab.harness.per_query import compare_experiment
        rd = _run_dir(run_id)
        return compare_experiment(rd.parent, rd.name, experiment_id, bootstrap=bootstrap, seed=seed)

    @router.get("/api/shared-tests/{test_id}")
    def get_shared_test(test_id: str, metric: Optional[str] = None,
                        reference: Optional[str] = None,
                        supertask_id: Optional[str] = None):
        """Every node, in every run under this root, scored on one shared test id."""
        from looplab.harness.per_query import shared_test_leaderboard
        run_ids = None
        if supertask_id:
            assignments = srv.projects.load()["supertask_assignments"]
            run_ids = sorted(r for r, s in assignments.items() if s == supertask_id)
        return shared_test_leaderboard(srv.root, test_id, metric=metric, reference=reference,
                                       run_ids=run_ids)

    def _supertask(sid: str) -> dict:
        data = srv.projects.load()
        st = next((s for s in data["supertasks"] if s.get("id") == sid), None)
        if st is None:
            raise HTTPException(404, f"no super-task {sid!r}")
        runs = sorted(r for r, s in data["supertask_assignments"].items() if s == sid)
        return {"supertask": st, "run_ids": runs}

    @router.get("/api/supertasks/{sid}/campaign")
    def get_campaign(sid: str, bootstrap: Optional[int] = Query(default=None, ge=100, le=20_000)):
        """The super-task's campaign report: computed comparisons + the authored narrative."""
        from looplab.harness.campaign_report import campaign_report
        found = _supertask(sid)
        return campaign_report(srv.root, found["supertask"], found["run_ids"], bootstrap=bootstrap)

    @router.post("/api/supertasks/{sid}/campaign/narrative")
    async def post_narrative(sid: str, body: NarrativeBody, request: Request):
        """Publish a new revision of the campaign narrative (compare-and-swap on the revision)."""
        from looplab.harness.campaign_report import publish_narrative
        agent = request_agent_token(request)

        def exists(supertask_id):
            return any(s.get("id") == supertask_id for s in srv.projects.load()["supertasks"])
        return await anyio.to_thread.run_sync(
            lambda: publish_narrative(srv.root, sid, body, agent_token=agent,
                                      supertask_exists=exists))

    return router
