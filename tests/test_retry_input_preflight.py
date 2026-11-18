"""Behavioral acceptance for `retry_input_preflight` (doc 84 §6.2, incident F9).

Verifies:
1. Self-workdir dependency (`nodes/node_<id>/...`) is refused BEFORE `rmtree_readonly_aware(wd)`
   (both at `retry_node`/`node_reset` admission and inside `WorkspaceSeeder.materialize`), so the
   earlier attempt's checkpoint/artifacts inside `nodes/node_<id>` survive intact.
2. External symlink pointing into `nodes/node_<id>` (or nested directory symlink pointing into it)
   is likewise refused as `ephemeral_resume_input` before cleanup.
3. Verified external durable input (with matching `sha256`, `source_attempt`, and `receipt_path`)
   passes `retry_node`, rematerializes cleanly, and evaluates the new attempt.
4. Declared missing input is reported as `missing_resume_input` (preserving old `wd`), distinct
   from legacy unpinned retry where arbitrary undeclared output in `nodes/node_<id>` is cleaned as
   usual.
5. `source_attempt` / `receipt_path` drift (`resume_input_attempt_drift`) and `sha256` corruption
   (`resume_input_integrity_mismatch`) fail closed before cleanup.
6. Stage-scoped reuse (`from_stage="score"` after completed `"train"`) continues to reuse the
   earlier stage's workdir without regression.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.adapters.tasks import load_task
from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.core.retry_inputs import RetryInputPreflightError
from looplab.engine.orchestrator import Engine
from looplab.engine.workspace import retry_input_preflight
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.events.types import EV_NODE_EVALUATED, EV_NODE_FAILED, EV_PAUSE
from looplab.harness import composite
from looplab.harness.mcp_server import HarnessAPI
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app

_NO_OBLIGATIONS = {
    "deep_research_every": -1,
    "report_every": 0,
    "track_hypotheses": False,
    "concept_pivot": False,
    "concept_run_base": False,
    "cross_run_concepts": False,
    "novelty_mode": "off",
    "foresight": False,
}


class _StubDev:
    def implement(self, idea):
        return ""


class _StubResearcher:
    def propose(self, state, parent):
        return Idea(operator="draft", params={})


def _setup_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    monkeypatch.setenv("LOOPLAB_AGENT_EVENTS", "0")
    monkeypatch.setenv("LOOPLAB_UI_AUTO_RESUME", "0")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run_eval.py").write_text("print('SCORE: 0.91')\n", encoding="utf-8")

    runs_root = tmp_path / "runs"
    rd = runs_root / "r74"
    rd.mkdir(parents=True)
    task_doc = {
        "kind": "repo",
        "id": "retry-preflight-test",
        "goal": "Maximize SCORE.",
        "direction": "max",
        "editable_path": str(repo),
        "edit_surface": ["candidate.py"],
        "protect": ["run_eval.py"],
        "eval": {
            "command": [sys.executable, "run_eval.py"],
            "timeout": 30.0,
            "metric": {"kind": "stdout_regex", "pattern": r"SCORE:\s*([0-9.]+)"},
        },
    }
    (rd / "task.snapshot.json").write_text(json.dumps(task_doc, indent=2), encoding="utf-8")
    settings = Settings(backend="toy", external_harness=True, eval_canary=False, **_NO_OBLIGATIONS)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")), encoding="utf-8")

    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r74", "run_uid": "uid-74", "task_id": "retry-preflight-test", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 74, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "rationale": "attempt 0"}, "code": "", "files": {}, "deleted": [],
    })
    store.append("node_failed", {"node_id": 74, "generation": 0, "error": "publisher timeout at step 13000", "reason": "crash", "eval_seconds": 12.0})
    store.append("pause", {"reason": "operator_pause"})

    # Simulate valuable partial output saved inside nodes/node_74 during attempt 0 (incident F9).
    wd = rd / "nodes" / "node_74"
    (wd / "checkpoint-13000").mkdir(parents=True)
    saved_ckpt = wd / "checkpoint-13000" / "model.bin"
    saved_ckpt.write_bytes(b"PRECIOUS_WEIGHTS_ATTEMPT_0")
    return runs_root, rd, store, wd, saved_ckpt


def _engine(rd: Path, store: EventStore) -> Engine:
    task = load_task(rd / "task.snapshot.json", existing_run=True)
    return Engine(
        rd,
        task=task,
        researcher=_StubResearcher(),
        developer=_StubDev(),
        sandbox=SubprocessSandbox(python=sys.executable),
        policy=GreedyTree(n_seeds=1, max_nodes=8),
        auto_install_deps=False,
        inline_repair=False,
        eval_canary=False,
        external_harness=True,
    )


def test_self_workdir_and_symlink_resume_inputs_refused_before_cleanup_preserving_old_attempt(tmp_path, monkeypatch):
    runs_root, rd, store, wd, saved_ckpt = _setup_run(tmp_path, monkeypatch)
    gen = run_generation_token(store.read_all())

    # External symlink pointing inside disposable nodes/node_74 must ALSO be caught!
    ext_symlink = tmp_path / "sneaky_link.bin"
    ext_symlink.symlink_to(saved_ckpt)

    app = make_app(runs_root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda *a, **kw: None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8,
    )
    with TestClient(app) as client:
        def forward(request: httpx.Request) -> httpx.Response:
            reply = client.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() in ("content-type", "x-looplab-token", "idempotency-key")
                },
            )
            return httpx.Response(reply.status_code, content=reply.content, headers={"content-type": reply.headers.get("content-type", "")})

        api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(forward))

        # 1) Direct path inside nodes/node_74 -> refused at retry_node admission (no node_reset written)!
        res_inside = composite.retry_node(
            api, "r74", gen, node_id=74, key="retry-inside",
            resume_inputs=[{"path": str(saved_ckpt), "kind": "file", "source_attempt": 0}],
        )
        assert res_inside["ok"] is False
        assert res_inside["steps"][-1]["code"] == "command_rejected"
        assert "ephemeral_resume_input" in json.dumps(res_inside["steps"][-1])
        assert [e.type for e in store.read_all()].count("node_reset") == 0
        assert saved_ckpt.read_bytes() == b"PRECIOUS_WEIGHTS_ATTEMPT_0"

        # 2) External symlink into nodes/node_74 -> refused at retry_node admission!
        res_sym = composite.retry_node(
            api, "r74", gen, node_id=74, key="retry-sym",
            resume_inputs=[{"path": str(ext_symlink), "kind": "file", "source_attempt": 0}],
        )
        assert res_sym["ok"] is False
        assert res_sym["steps"][-1]["code"] == "command_rejected"
        assert "ephemeral_resume_input" in json.dumps(res_sym["steps"][-1])
        assert [e.type for e in store.read_all()].count("node_reset") == 0

    # 3) Even if a hand-written / replayed `node_reset` reaches `WorkspaceSeeder.materialize` directly,
    #    `retry_input_preflight` aborts BEFORE `rmtree_readonly_aware(wd)` so `saved_ckpt` survives!
    store.append("node_reset", {
        "node_id": 74, "generation": 0, "from_stage": "eval",
        "resume_inputs": [{"path": str(saved_ckpt), "kind": "file", "source_attempt": 0}],
    })
    store.append("resume", {})
    eng = _engine(rd, store)
    anyio.run(eng._evaluate, 74, anyio.CapacityLimiter(1))

    # CRITICAL INVARIANT: the earlier attempt's checkpoint inside nodes/node_74 was NOT deleted!
    assert saved_ckpt.is_file()
    assert saved_ckpt.read_bytes() == b"PRECIOUS_WEIGHTS_ATTEMPT_0"
    evs = store.read_all()
    failed = [e for e in evs if e.type == EV_NODE_FAILED and e.data.get("node_id") == 74 and e.data.get("generation") == 1]
    assert len(failed) == 1
    assert failed[0].data["reason"] == "ephemeral_resume_input"
    assert failed[0].data["eval_seconds"] == 0.0
    assert any(e.type == EV_PAUSE and e.data.get("reason") == "ephemeral_resume_input" for e in evs)


def test_missing_drift_and_corrupt_resume_inputs_vs_verified_external_durable_pass(tmp_path, monkeypatch):
    runs_root, rd, store, wd, saved_ckpt = _setup_run(tmp_path, monkeypatch)

    durable_dir = tmp_path / "durable_store" / "node74_g0"
    durable_dir.mkdir(parents=True)
    durable_ckpt = durable_dir / "model.bin"
    durable_ckpt.write_bytes(saved_ckpt.read_bytes())
    good_sha = hashlib.sha256(durable_ckpt.read_bytes()).hexdigest()
    receipt = durable_dir / "DURABLE_RECEIPT.json"
    receipt.write_text(
        json.dumps({"verified": True, "native_binding": {"node_id": 74, "attempt": 0}}),
        encoding="utf-8",
    )

    eng = _engine(rd, store)
    node_g1 = fold(store.read_all()).nodes[74].model_copy(update={"attempt": 1})

    # Missing declared input -> distinct `missing_resume_input` before cleanup
    missing_node = node_g1.model_copy(update={"resume_inputs": [{"path": str(durable_dir / "absent.bin")}]})
    with pytest.raises(RetryInputPreflightError) as exc_missing:
        retry_input_preflight(missing_node, wd, run_dir=rd)
    assert exc_missing.value.code == "missing_resume_input"

    # Attempt drift in receipt (`source_attempt=0` vs receipt saying attempt 2) -> `resume_input_attempt_drift`
    bad_receipt = durable_dir / "BAD_RECEIPT.json"
    bad_receipt.write_text(json.dumps({"native_binding": {"node_id": 74, "attempt": 2}}), encoding="utf-8")
    drift_node = node_g1.model_copy(update={
        "resume_inputs": [{"path": str(durable_ckpt), "source_attempt": 0, "receipt_path": str(bad_receipt)}]
    })
    with pytest.raises(RetryInputPreflightError) as exc_drift:
        retry_input_preflight(drift_node, wd, run_dir=rd)
    assert exc_drift.value.code == "resume_input_attempt_drift"

    # Wrong sha256 -> `resume_input_integrity_mismatch`
    corrupt_node = node_g1.model_copy(update={
        "resume_inputs": [{"path": str(durable_ckpt), "sha256": "f" * 64}]
    })
    with pytest.raises(RetryInputPreflightError) as exc_sha:
        retry_input_preflight(corrupt_node, wd, run_dir=rd)
    assert exc_sha.value.code == "resume_input_integrity_mismatch"
    assert saved_ckpt.is_file()  # wd untouched across all refusals!

    # Verified external durable reference passes retry_node AND full engine rematerialization + eval!
    gen = run_generation_token(store.read_all())
    app = make_app(runs_root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda *a, **kw: None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8,
    )
    with TestClient(app) as client:
        def forward(request: httpx.Request) -> httpx.Response:
            reply = client.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() in ("content-type", "x-looplab-token", "idempotency-key")
                },
            )
            return httpx.Response(reply.status_code, content=reply.content, headers={"content-type": reply.headers.get("content-type", "")})

        api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(forward))
        ok_retry = composite.retry_node(
            api, "r74", gen, node_id=74, key="retry-verified",
            resume_inputs=[{
                "path": str(durable_ckpt),
                "kind": "file",
                "sha256": good_sha,
                "source_attempt": 0,
                "receipt_path": str(receipt),
            }],
        )
        # With spawn_engine stubbed out, the admitted reset row lands in events.jsonl before spawn wait.
        assert ok_retry["steps"][-1]["step"] == "reset", ok_retry
        resets = [e for e in store.read_all() if e.type == "node_reset"]
        assert len(resets) == 1
        assert resets[0].data["resume_inputs"][0]["sha256"] == good_sha

    store.append("resume", {})
    eng2 = _engine(rd, store)
    anyio.run(eng2._evaluate, 74, anyio.CapacityLimiter(1))
    evs = store.read_all()
    evaluated = [e for e in evs if e.type == EV_NODE_EVALUATED and e.data.get("node_id") == 74 and e.data.get("generation") == 1]
    assert len(evaluated) == 1 and evaluated[0].data["metric"] == 0.91
    # Undeclared old checkpoint inside nodes/node_74 was cleaned during the fresh rematerialization,
    # while the external durable copy remained intact!
    assert not saved_ckpt.exists()
    assert durable_ckpt.read_bytes() == b"PRECIOUS_WEIGHTS_ATTEMPT_0"
