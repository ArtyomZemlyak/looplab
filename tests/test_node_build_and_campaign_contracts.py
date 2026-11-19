"""Regression and contract tests for live external-campaign control-plane hazards (F6, F10, job_role)."""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.adapters.tasks import load_task
from looplab.core.config import Settings
from looplab.engine.node_build import NodeBuildMixin
from looplab.engine.orchestrator import Engine
from looplab.engine.shared import run_dir_evaluator_digest
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from looplab.serve.server import make_app

ROOT = Path(__file__).resolve().parents[1]
AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "owner-secret"}


def _dict_string_keys(node: ast.AST) -> set[str]:
    """Extract constant string keys from dict literals, including conditional `{k: v} if c else {}`."""
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if isinstance(node, ast.IfExp):
        return _dict_string_keys(node.body) | _dict_string_keys(node.orelse)
    return set()


def test_every_engine_node_created_caller_passes_only_declared_keywords():
    """Prevent the live nid=9/36/85 class of crashes where `_commit_built_node` forwarded a keyword
    (`job_role`) that `NodeBuildMixin._emit_node_created` did not accept."""
    emit_params = {
        name for name, p in inspect.signature(NodeBuildMixin._emit_node_created).parameters.items()
        if name != "self" and p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    commit_own = {
        name for name, p in inspect.signature(NodeBuildMixin._commit_built_node).parameters.items()
        if name != "self" and p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    allowed_for_commit = commit_own | emit_params

    bad: list[str] = []
    for path in sorted((ROOT / "looplab" / "engine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr not in ("_emit_node_created", "_commit_built_node"):
                continue
            allowed = emit_params if attr == "_emit_node_created" else allowed_for_commit
            passed: set[str] = set()
            for kw in node.keywords:
                if kw.arg is not None:
                    passed.add(kw.arg)
                else:
                    passed |= _dict_string_keys(kw.value)
            unknown = sorted(passed - allowed)
            if unknown:
                bad.append(f"{path.name}:{node.lineno} {attr} passes unknown {unknown}")
    assert not bad, "\n".join(bad)


def _stub_roles():
    class _R:
        def propose(self, *a, **kw):
            raise AssertionError("external_harness should not call propose")

    class _D:
        def implement(self, *a, **kw):
            return ""

    return _R(), _D()


def test_running_engine_inject_materializes_job_role_and_evaluator_digest_without_build_crash(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "score.py").write_text('import json\nprint(json.dumps({"score": 1.0}))\n', encoding="utf-8")
    (repo / "notes.txt").write_text("seed\n", encoding="utf-8")
    task_data = {
        "kind": "repo",
        "id": "inject_job_role",
        "goal": "fit artifact test",
        "direction": "max",
        "editable_path": str(repo),
        "edit_surface": ["notes.txt"],
        "protect": ["score.py"],
        "eval": {
            "command": ["python", "score.py"],
            "metric": {"kind": "stdout_json", "key": "score"},
        },
    }
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(json.dumps(task_data), encoding="utf-8")
    task = load_task(rd / "task.snapshot.json", existing_run=True)
    researcher, developer = _stub_roles()
    eng = Engine(
        rd,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(python=sys.executable),
        policy=GreedyTree(n_seeds=1, max_nodes=4),
        auto_install_deps=False,
        inline_repair=False,
        external_harness=True,
    )
    eng.store.append("run_started", {
        "run_id": "inject_job_role", "run_uid": "uid-jr-1", "task_id": "inject_job_role",
        "goal": "fit artifact test", "direction": "max",
    })
    digest = run_dir_evaluator_digest(rd, run_uid="uid-jr-1")
    assert digest and len(digest) == 64

    eng.store.append("inject_node", {
        "operator": "fit_lastquery",
        "rationale": "run 1M fit",
        "hypothesis": "train history cut",
        "files": {"notes.txt": "arm=last_query\n"},
        "node_kind": "artifact",
        "job_role": "fit",
        "expected_evaluator_digest": digest,
    })
    pending_inject = next(e.data for e in eng.store.read_all() if e.type == "inject_node")
    eng._create_injected_node(pending_inject)

    events = eng.store.read_all()
    assert not [e for e in events if e.type == "inject_failed"]
    folded = fold(events)
    node = folded.nodes[0]
    assert node.kind == "artifact"
    assert node.job_role == "fit"
    assert node.expected_evaluator_digest == digest


def _setup_campaign_app(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    root = tmp_path / "runs"
    root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "score.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "exp.env").write_text("ARM=0\n", encoding="utf-8")

    rd = root / "demo"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text(
        Settings(backend="toy", external_harness=True).model_dump_json(), encoding="utf-8"
    )
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo", "id": "demo", "direction": "max", "goal": "g",
        "editable_path": str(repo), "edit_surface": ["exp.env"], "protect": ["score.py"],
        "eval": {"command": ["python", "score.py"],
                 "metric": {"kind": "stdout_json", "key": "recall"}, "inputs": []},
    }, indent=2), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "uid-demo",
                                 "task_id": "demo", "goal": "g", "direction": "max"})
    monkeypatch.setattr("looplab.serve.engine_proc.auto_resume_enabled", lambda: False)
    app = make_app(root)
    client = TestClient(app)
    monkeypatch.setattr(client.app.state.looplab.commands, "engine_liveness", lambda _rd: False)
    return rd, store, client


@pytest.fixture(autouse=True)
def _reap_test_spawned_engines():
    from looplab.serve import engine_proc
    before = set(engine_proc._spawned_engines)
    yield
    for pid in list(set(engine_proc._spawned_engines) - before):
        proc = engine_proc._spawned_engines.pop(pid, None)
        engine_proc._kill_process_tree(pid)
        if proc is not None and callable(getattr(proc, "wait", None)):
            import subprocess
            try:
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


def test_budget_protocol_amendment_accepts_eval_timeout_and_max_eval_seconds(tmp_path, monkeypatch):
    rd, store, client = _setup_campaign_app(tmp_path, monkeypatch)
    gen = run_generation_token(store.read_all())
    url = "/api/runs/demo/protocol/amendments"

    # Combined multi-field budget amendment (the exact shape F10 hit in live campaigns).
    r = client.post(url, headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "bud-1",
        "kind": "budget",
        "reason": "Extend eval timeout and total eval budget for 4-GPU 1M fit",
        "budget": {"add_nodes": 12, "eval_timeout": 129700.0, "max_eval_seconds": 2400000.0},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["replayed"] is False
    payload = body["amendment"]["payload"]
    assert payload["add_nodes"] == 12
    assert payload["eval_timeout"] == 129700.0
    assert payload["max_eval_seconds"] == 2400000.0
    assert "eval_timeout=129700s" in body["amendment"]["summary"]

    # Idempotent replay on same action_id; 409 on mutated body.
    rep = client.post(url, headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "bud-1",
        "kind": "budget",
        "reason": "Extend eval timeout and total eval budget for 4-GPU 1M fit",
        "budget": {"add_nodes": 12, "eval_timeout": 129700.0, "max_eval_seconds": 2400000.0},
    })
    assert rep.status_code == 200 and rep.json()["replayed"] is True

    conflict = client.post(url, headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "bud-1",
        "kind": "budget",
        "reason": "Extend eval timeout and total eval budget for 4-GPU 1M fit",
        "budget": {"add_nodes": 12, "eval_timeout": 90000.0, "max_eval_seconds": 2400000.0},
    })
    assert conflict.status_code == 409

    # Solo eval_timeout amendment without add_nodes also works cleanly; >7d refused with 400.
    gen2 = run_generation_token(store.read_all())
    solo = client.post(url, headers=AGENT, json={
        "expected_generation": gen2,
        "action_id": "bud-2",
        "kind": "budget",
        "reason": "Lift eval_timeout alone before long multi-cohort score",
        "budget": {"eval_timeout": 14400.0},
    })
    assert solo.status_code == 200, solo.text
    assert solo.json()["amendment"]["payload"]["eval_timeout"] == 14400.0

    over = client.post(url, headers=AGENT, json={
        "expected_generation": run_generation_token(store.read_all()),
        "action_id": "bud-over",
        "kind": "budget",
        "reason": "Unbounded timeout above 7 days must fail closed",
        "budget": {"eval_timeout": 700000.0},
    })
    assert over.status_code == 400

    # Operator policy amendments=off blocks agent eval_timeout extension too.
    assert client.put("/api/runs/demo/harness-policy", headers=OWNER,
                      json={"amendments": "off"}).status_code == 200
    denied = client.post(url, headers=AGENT, json={
        "expected_generation": run_generation_token(store.read_all()),
        "action_id": "bud-denied",
        "kind": "budget",
        "reason": "Policy off should block agent timeout increase",
        "budget": {"eval_timeout": 3600.0},
    })
    assert denied.status_code == 403


def test_task_amendment_accepts_large_multi_shard_input_closures_above_256(tmp_path, monkeypatch):
    rd, store, client = _setup_campaign_app(tmp_path, monkeypatch)
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()
    paths = []
    for i in range(320):
        p = shards_dir / f"shard-{i:04d}.bin"
        p.write_bytes(b"ok")
        paths.append(str(p))

    gen = run_generation_token(store.read_all())
    url = "/api/runs/demo/protocol/amendments"
    r = client.post(url, headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "task-320",
        "kind": "task",
        "reason": "Bind 320 shard and source closure paths in one atomic task amendment",
        "task": {"inputs_add": paths},
    })
    assert r.status_code == 200, r.text
    snap = json.loads((rd / "task.snapshot.json").read_text(encoding="utf-8"))
    assert len(snap["eval"]["inputs"]) == 320

    r_rm = client.post(url, headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "task-320-rm",
        "kind": "task",
        "reason": "Remove 320 shard paths cleanly in one atomic amendment",
        "task": {"inputs_remove": paths},
    })
    assert r_rm.status_code == 200, r_rm.text


def test_stopped_remediation_and_harness_progress_distinguish_pause_from_stop(tmp_path, monkeypatch):
    rd, store, client = _setup_campaign_app(tmp_path, monkeypatch)
    store.append("pause", {"reason": "paused under GPU hold"})
    gen = run_generation_token(store.read_all())

    # Simulate F6: run is paused in events.jsonl, while idle engine loop is still alive!
    monkeypatch.setattr(client.app.state.looplab.commands, "engine_liveness", lambda _rd: True)
    monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _rd: True)

    refused = client.post("/api/runs/demo/protocol/amendments", headers=AGENT, json={
        "expected_generation": gen,
        "action_id": "task-while-paused-running",
        "kind": "task",
        "reason": "Attempt task amendment while paused but engine still running",
        "task": {"protect_add": ["exp.env"]},
    })
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "engine_running"
    assert "/stop" in detail["remediation"] and "looplab stop" in detail["remediation"]

    prog = client.get("/api/runs/demo/harness-progress",
                      params={"expected_generation": gen}, headers=AGENT).json()
    assert prog["recorded_lifecycle"]["paused"] is True
    assert prog["execution"]["engine_running"] is True
    assert prog["execution"]["task_or_files_amendable"] is False
    assert "looplab stop" in prog["execution"]["stop_vs_pause_note"]
    assert prog["runtime_build"]["package_version"]
    assert prog["runtime_build"]["git_head"] is None or len(prog["runtime_build"]["git_head"]) == 40

    brief = client.get("/api/runs/demo/harness-progress",
                       params={"expected_generation": gen, "brief": "true"}, headers=AGENT).json()
    assert brief["execution"]["task_or_files_amendable"] is False
    assert "stop_vs_pause_note" in brief["execution"]
    assert brief["runtime_build"] == prog["runtime_build"]
