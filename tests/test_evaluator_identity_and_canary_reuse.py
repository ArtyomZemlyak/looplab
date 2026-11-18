"""Behavioral acceptance for canonical evaluator identity and cross-node canary reuse.

Covers:
1. `expected_evaluator_digest` (`docs/84` §6.1):
   - `harness-progress` (full and `brief=true`) exposes `current_evaluator_digest`;
   - matching `expected_evaluator_digest` passes `submit_experiment` / `inject_node` and evaluates;
   - omitted `expected_evaluator_digest` preserves legacy admission and marks `unpinned_legacy` in dry_run;
   - stale `expected_evaluator_digest` fails closed (`409 stale_evaluator_digest`) before sidecar writes;
   - exact idempotent retry of an already-accepted `key:inject` replays its receipt even after the
     task's evaluator is amended;
   - race amendment (`amend_protocol(kind="task")` or protected file change `kind="files"`) between
     candidate admission and engine dispatch fails closed before sandbox/GPU work (`eval_seconds == 0.0`,
     `reason == "stale_evaluator_digest"`, run paused, old scorer never executed).
2. Cross-node `eval_canary` equivalence reuse (`docs/69` 69.31):
   - sibling node with identical code, substrate, evaluator digest, canary spec, `node_kind` and
     `uses` pins reuses a clean passed canary (`["canary", "full", "full"]`);
   - code change, evaluator/env change, protected-file change, `node_kind` change, `node_reset` or
     `inconclusive` smoke invalidates reuse and runs a fresh canary.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
from fastapi.testclient import TestClient

from looplab.adapters.tasks import load_task
from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.eval_canary import canary_already_passed
from looplab.engine.orchestrator import Engine
from looplab.engine.shared import run_dir_evaluator_digest
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.events.types import EV_EVAL_CANARY_FINISHED, EV_NODE_FAILED, EV_PAUSE
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


def _make_external_repo_run(tmp_path: Path, monkeypatch) -> tuple[Path, Path, EventStore, str]:
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    monkeypatch.setenv("LOOPLAB_AGENT_EVENTS", "0")
    monkeypatch.setenv("LOOPLAB_UI_AUTO_RESUME", "0")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "evaluate.py").write_text(
        "import os\n"
        "marker = os.environ.get('MARKER_FILE', '')\n"
        "if marker:\n"
        "    with open(marker, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(('canary' if os.environ.get('LOOPLAB_CANARY') == '1' else 'full') + ':v1\\n')\n"
        "print('SCORE: 0.75')\n",
        encoding="utf-8",
    )
    (repo / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")

    marker = tmp_path / "executions.log"
    runs_root = tmp_path / "runs"
    rd = runs_root / "exp1"
    rd.mkdir(parents=True)

    task_doc = {
        "kind": "repo",
        "id": "repo-eval-id-test",
        "goal": "Maximize SCORE safely.",
        "direction": "max",
        "editable_path": str(repo),
        "edit_surface": ["candidate.py"],
        "protect": ["evaluate.py"],
        "eval": {
            "command": [sys.executable, "evaluate.py"],
            "timeout": 30.0,
            "env": {"MARKER_FILE": str(marker)},
            "canary": {"timeout": 15.0, "env": {"SLICE": "tiny"}},
            "metric": {"kind": "stdout_regex", "pattern": r"SCORE:\s*([0-9.]+)"},
        },
    }
    (rd / "task.snapshot.json").write_text(json.dumps(task_doc, indent=2), encoding="utf-8")
    settings = Settings(backend="toy", external_harness=True, eval_canary=True, **_NO_OBLIGATIONS)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append(
        "run_started",
        {"run_id": "exp1", "run_uid": "uid-eval-1", "task_id": "repo-eval-id-test", "goal": "g", "direction": "max"},
    )
    store.append("pause", {"reason": "operator_pause"})
    gen = run_generation_token(store.read_all())
    return runs_root, rd, store, gen


def _build_engine(rd: Path, store: EventStore) -> Engine:
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
        eval_canary=True,
        external_harness=True,
    )


def _all_decisions(prog: dict) -> dict:
    return {
        phase: {"reason": f"{phase} checked", "alternatives": [{"idea": "keep baseline", "why_not": "worse"}]}
        for phase in (prog.get("candidate_decisions_per_idea") or {})
    }


def test_submit_experiment_pins_evaluator_digest_refuses_stale_and_replays_idempotently(tmp_path, monkeypatch):
    runs_root, rd, store, gen = _make_external_repo_run(tmp_path, monkeypatch)
    app = make_app(runs_root)
    srv = app.state.looplab
    spawns: list = []
    srv.commands = RunCommandService(
        srv,
        spawn_engine=lambda args, env=None, run_dir=None: spawns.append(list(args)) or None,
        startup_timeout=0.2,
        command_timeout=0.4,
        poll_interval=0.01,
        max_observation_timeout=0.8,
    )
    agent_headers = {"X-LoopLab-Token": "agent-secret"}
    owner_headers = {"X-LoopLab-Token": "owner-secret"}

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
            return httpx.Response(
                reply.status_code,
                content=reply.content,
                headers={"content-type": reply.headers.get("content-type", "")},
            )

        api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(forward))
        prog_full = client.get(
            "/api/runs/exp1/harness-progress",
            params={"expected_generation": gen},
            headers=agent_headers,
        ).json()
        prog_brief = client.get(
            "/api/runs/exp1/harness-progress",
            params={"expected_generation": gen, "brief": True},
            headers=agent_headers,
        ).json()
        digest_v1 = prog_full["current_evaluator_digest"]
        assert isinstance(digest_v1, str) and len(digest_v1) == 64
        assert prog_brief["current_evaluator_digest"] == digest_v1

        # Legacy omission explicitly reports unpinned_legacy in dry_run validation.
        dry_legacy = composite.submit_experiment(
            api, "exp1", gen, key="dry-legacy",
            idea={"operator": "draft", "rationale": "unpinned check"},
            files={"candidate.py": "VALUE = 2\n"},
            decisions=_all_decisions(prog_full),
            dry_run=True,
        )
        assert dry_legacy["ok"] is True, dry_legacy
        assert dry_legacy["steps"][-1]["evaluator_binding"] == "unpinned_legacy"

        # Stale digest is refused fail-closed before writing decisions or inject commands.
        stale = "0" * 64
        refused = composite.submit_experiment(
            api, "exp1", gen, key="stale-cand",
            idea={"operator": "draft", "rationale": "stale check"},
            files={"candidate.py": "VALUE = 2\n"},
            decisions=_all_decisions(prog_full),
            expected_evaluator_digest=stale,
        )
        assert refused["ok"] is False
        assert refused["steps"][-1]["code"] == "stale_evaluator_digest"
        assert not (rd / "harness_decisions.jsonl").exists()

        # Direct HTTP command intake also refuses stale expected_evaluator_digest with 409.
        direct_bad = client.post(
            "/api/runs/exp1/commands",
            headers={**agent_headers, "Idempotency-Key": "direct-stale"},
            json={
                "expected_generation": gen,
                "type": "inject_node",
                "data": {
                    "idea": {"operator": "draft", "rationale": "direct stale"},
                    "files": {"candidate.py": "VALUE = 2\n"},
                    "expected_evaluator_digest": stale,
                },
            },
        )
        assert direct_bad.status_code in (200, 409)
        if direct_bad.status_code == 409:
            assert direct_bad.json()["detail"]["code"] == "stale_evaluator_digest"
        else:
            assert direct_bad.json()["status"] == "rejected"

        # Matching digest succeeds and records expected_evaluator_digest on the queued inject.
        accepted = composite.submit_experiment(
            api, "exp1", gen, key="good-cand",
            idea={"operator": "draft", "rationale": "pinned check"},
            files={"candidate.py": "VALUE = 3\n"},
            decisions=_all_decisions(prog_full),
            expected_evaluator_digest=digest_v1,
        )
        assert accepted["ok"] is True, accepted

        # Amend protected scorer file while engine is stopped -> current_evaluator_digest changes!
        eval_py = tmp_path / "repo" / "evaluate.py"
        before_sha = hashlib.sha256(eval_py.read_bytes()).hexdigest()
        amend_res = client.post(
            "/api/runs/exp1/protocol/amendments",
            headers=owner_headers,
            json={
                "expected_generation": gen,
                "action_id": "amend-scorer-v2",
                "kind": "files",
                "reason": "Switch scorer to v2 protocol",
                "files": [{
                    "path": "evaluate.py",
                    "expected_sha256": before_sha,
                    "content": eval_py.read_text(encoding="utf-8").replace(":v1", ":v2"),
                }],
            },
        )
        assert amend_res.status_code == 200, amend_res.text
        digest_v2 = run_dir_evaluator_digest(rd, run_uid="uid-eval-1")
        assert digest_v2 is not None and digest_v2 != digest_v1

        # Exact idempotent retry of the ALREADY-accepted key replays its receipt cleanly.
        replayed = composite.submit_experiment(
            api, "exp1", gen, key="good-cand",
            idea={"operator": "draft", "rationale": "pinned check"},
            files={"candidate.py": "VALUE = 3\n"},
            decisions=_all_decisions(prog_full),
            expected_evaluator_digest=digest_v1,
        )
        assert replayed["ok"] is True, replayed

        # Mutating expected_evaluator_digest under the SAME already-accepted key is refused (409).
        mutated_replay = composite.submit_experiment(
            api, "exp1", gen, key="good-cand",
            idea={"operator": "draft", "rationale": "pinned check"},
            files={"candidate.py": "VALUE = 3\n"},
            decisions=_all_decisions(prog_full),
            expected_evaluator_digest=digest_v2,
        )
        assert mutated_replay["ok"] is False
        assert mutated_replay["stopped_at"] == "inject"

    # Dispatch race check (F3): when the engine materializes and attempts to evaluate the
    # queued v1 candidate against the v2 scorer on disk, it fails closed BEFORE running the
    # scorer or canary (`executions.log` stays empty, `eval_seconds == 0.0`, run pauses).
    store.append("resume", {})
    eng = _build_engine(rd, store)
    pending_inject = next(e.data for e in store.read_all() if e.type == "inject_node")
    eng._create_injected_node(pending_inject)
    created_node = fold(store.read_all()).nodes[0]
    assert created_node.expected_evaluator_digest == digest_v1

    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1))
    evs = store.read_all()
    failed = [e for e in evs if e.type == EV_NODE_FAILED and e.data.get("node_id") == 0]
    assert len(failed) == 1
    assert failed[0].data["reason"] == "stale_evaluator_digest"
    assert failed[0].data["eval_seconds"] == 0.0
    pauses = [e for e in evs if e.type == EV_PAUSE and e.data.get("reason") == "stale_evaluator_digest"]
    assert pauses, "stale evaluator mismatch must pause the run before executing"
    assert not (tmp_path / "executions.log").exists(), "stale scorer must never execute"


def test_cross_node_canary_reuses_only_under_full_contract_equivalence(tmp_path, monkeypatch):
    _runs_root, rd, store, _gen = _make_external_repo_run(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

    store.append("resume", {})
    eng = _build_engine(rd, store)
    digest_v1 = run_dir_evaluator_digest(rd, run_uid="uid-eval-1")

    for nid in (0, 1):
        eng._emit_node_created(
            node_id=nid, parent_ids=[], operator="draft",
            idea=Idea(operator="draft", rationale=f"arm {nid}").model_dump(),
            code="", files={"candidate.py": "VALUE = 10\n"}, deleted=[],
            source="manual", origin=None, eval_start_boundary=True,
            expected_evaluator_digest=digest_v1,
        )

    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1))
    anyio.run(eng._evaluate, 1, anyio.CapacityLimiter(1))

    marker_lines = (tmp_path / "executions.log").read_text(encoding="utf-8").splitlines()
    # Node 0 ran canary + full; Node 1 reused Node 0's passed canary and ran only full!
    assert marker_lines == ["canary:v1", "full:v1", "full:v1"]
    canary_rows = [e for e in store.read_all() if e.type == EV_EVAL_CANARY_FINISHED]
    assert len(canary_rows) == 1 and canary_rows[0].data["equivalence_digest"]

    # Node 2 changes candidate code -> must NOT reuse Node 0's canary!
    eng._emit_node_created(
        node_id=2, parent_ids=[], operator="draft",
        idea=Idea(operator="draft", rationale="changed code").model_dump(),
        code="", files={"candidate.py": "VALUE = 999\n"}, deleted=[],
        source="manual", origin=None, eval_start_boundary=True,
        expected_evaluator_digest=digest_v1,
    )
    anyio.run(eng._evaluate, 2, anyio.CapacityLimiter(1))
    marker_lines = (tmp_path / "executions.log").read_text(encoding="utf-8").splitlines()
    assert marker_lines == ["canary:v1", "full:v1", "full:v1", "canary:v1", "full:v1"]

    # Node 3 has same code as Node 0, BUT protected scorer changes on disk -> evaluator_digest moves,
    # so Node 3 MUST run a new canary rather than reusing Node 0's v1 canary!
    eval_py = repo / "evaluate.py"
    eval_py.write_text(eval_py.read_text(encoding="utf-8").replace(":v1", ":v2"), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "evaluate.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "v2 scorer"], check=True)
    digest_v2 = run_dir_evaluator_digest(rd, run_uid="uid-eval-1")
    assert digest_v2 != digest_v1

    eng._emit_node_created(
        node_id=3, parent_ids=[], operator="draft",
        idea=Idea(operator="draft", rationale="same candidate on v2 scorer").model_dump(),
        code="", files={"candidate.py": "VALUE = 10\n"}, deleted=[],
        source="manual", origin=None, eval_start_boundary=True,
        expected_evaluator_digest=digest_v2,
    )
    anyio.run(eng._evaluate, 3, anyio.CapacityLimiter(1))
    marker_lines = (tmp_path / "executions.log").read_text(encoding="utf-8").splitlines()
    assert marker_lines[-2:] == ["canary:v2", "full:v2"]


def test_canary_already_passed_rejects_inconclusive_reset_and_aborted_donors():
    class _E:
        def __init__(self, etype, data):
            self.type, self.data = etype, data

    eq = "e" * 64
    inconclusive_logs = [
        _E(EV_EVAL_CANARY_FINISHED, {
            "node_id": 0, "generation": 0, "attempt": 0, "code_digest": "c0",
            "equivalence_digest": eq, "passed": True, "inconclusive": "refused write",
            "eval_seconds": 0.2,
        })
    ]
    assert not canary_already_passed(inconclusive_logs, 1, 0, "c0", equivalence_digest=eq)

    reset_logs = [
        _E(EV_EVAL_CANARY_FINISHED, {
            "node_id": 0, "generation": 0, "attempt": 0, "code_digest": "c0",
            "equivalence_digest": eq, "passed": True, "eval_seconds": 0.2,
        }),
        _E("node_reset", {"node_id": 0}),
    ]
    assert not canary_already_passed(reset_logs, 1, 0, "c0", equivalence_digest=eq)
    assert not canary_already_passed(reset_logs, 0, 1, "c0", equivalence_digest=eq)

    aborted_logs = [
        _E(EV_EVAL_CANARY_FINISHED, {
            "node_id": 0, "generation": 0, "attempt": 0, "code_digest": "c0",
            "equivalence_digest": eq, "passed": True, "eval_seconds": 0.2,
        }),
        _E("node_abort", {"node_id": 0}),
    ]
    assert not canary_already_passed(aborted_logs, 1, 0, "c0", equivalence_digest=eq)
