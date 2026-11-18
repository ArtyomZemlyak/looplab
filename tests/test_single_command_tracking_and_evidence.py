"""Regression tests for single-command `--config` / `eval.log` capture, command-referenced
evaluator identity, historical `runtime-params` `--config` attribution, `job_role` research
quality classification, and bidirectional `research_map` evidence links."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.core.effective_config import canonical_key, capture
from looplab.core.evaluator_identity import evaluator_digest_from_specs
from looplab.core.npyio import write_npy, write_npz
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.harness.research_quality import project as research_quality
from looplab.serve import runtime_params as rp
from looplab.serve.server import make_app


def test_evaluator_digest_tracks_external_command_config_and_fails_closed_on_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ext_cfg = tmp_path / "FIRST_CONFIG_86G0.json"
    ext_cfg.write_text(json.dumps({"steps": 977, "global_batch": 1024}), encoding="utf-8")
    spec = {
        "command": ["/usr/bin/timeout", "100s", "python", "-B", "run.py", "--config", str(ext_cfg)],
    }
    repo_spec = {"editables": [{"name": ".", "path": str(repo), "protect": []}]}
    d1 = evaluator_digest_from_specs(eval_spec=spec, repo_spec=repo_spec)
    assert len(d1) == 64

    ext_cfg.write_text(json.dumps({"steps": 2000, "global_batch": 1024}), encoding="utf-8")
    d2 = evaluator_digest_from_specs(eval_spec=spec, repo_spec=repo_spec)
    assert len(d2) == 64 and d2 != d1

    # Symlink `--config` fails closed (empty digest), matching R1-R3.
    link_cfg = tmp_path / "link.json"
    link_cfg.symlink_to(ext_cfg)
    spec_link = {"command": ["python", "run.py", "--config", str(link_cfg)]}
    assert evaluator_digest_from_specs(eval_spec=spec_link, repo_spec=repo_spec) == ""


def test_effective_config_captures_external_json_config_updates_cuda_env_and_eval_log(tmp_path):
    assert canonical_key("updates") == "max_steps"
    assert canonical_key("scientific_identity_updates") == "max_steps"

    workdir = tmp_path / "node_86"
    workdir.mkdir()
    ext_cfg = tmp_path / "FIT_CONFIG.json"
    ext_cfg.write_text(json.dumps({
        "global_batch": 1024,
        "epochs": 1,
        "scientific_identity": {"seed": 42, "updates": 39873},
        "expected_native_budget": {"max_eval_seconds": 2400000.0, "eval_timeout": 129700.0},
        "inputs": [{"path": "/tmp/shard0.bin", "sha256": "a" * 64}],
    }), encoding="utf-8")
    (workdir / "eval.log").write_text("LR: 2e-4\nStep 1: loss=10.8\n", encoding="utf-8")

    rec = capture(
        workdir,
        env={"CUDA_VISIBLE_DEVICES": "0,1,2,3"},
        overlay_files=["candidate-notes/note.md"],
        stage_names=[],
        declared={"global_batch": 1024, "epochs": 1, "seed": 42, "updates": 39873},
        command=["/usr/bin/timeout", "10500s", "python", "fit.py", "--config", str(ext_cfg)],
    )
    assert rec is not None
    eff = rec["effective"]
    assert eff["world_size"] == 4.0
    assert eff["batch_size"] == 1024.0
    assert eff["epochs"] == 1.0
    assert eff["seed"] == 42.0
    assert eff["max_steps"] == 39873.0
    assert eff["lr"] == 2e-4
    assert rec["checked"] == 4 and rec["unknown"] == [] and rec["mismatches"] == []
    assert f"config:{ext_cfg}" in rec["sources_read"]
    assert "log:eval.log" in rec["sources_read"]


def test_runtime_params_attributes_eval_command_config_only_to_matching_era(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_ANONYMOUS", "1")
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    cfg_old = tmp_path / "cfg_node0.json"
    cfg_new = tmp_path / "cfg_node1.json"
    cfg_old.write_text(json.dumps({"steps": 500, "api_token": "secret-tok-123456"}), encoding="utf-8")
    cfg_new.write_text(json.dumps({"steps": 977, "global_batch": 1024}), encoding="utf-8")

    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True).model_dump_json(), encoding="utf-8")
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo", "id": "demo", "direction": "max", "goal": "g",
        "editable_path": str(tmp_path), "edit_surface": ["candidate-notes/**"],
        "eval": {"command": ["python", "fit.py", "--config", str(cfg_new)],
                 "metric": {"kind": "stdout_json", "key": "m"}},
    }), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "u1", "task_id": "demo", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "fit0",
                                  "idea": {"operator": "fit0", "params": {"steps": 500}},
                                  "code": "", "files": {"candidate-notes/n0.txt": "old"}})
    store.append("node_evaluated", {"node_id": 0, "metric": None})
    # Task changes AFTER node 0, switching --config from cfg_old to cfg_new for node 1.
    t_chg = store.append("task_changed", {})
    (rd / "protocol_amendments.jsonl").write_text(
        json.dumps({
            "amendment_id": "am1", "version": 1, "kind": "task", "run_uid": "u1",
            "at_event_seq": t_chg.seq,
            "payload": {"changes": {"eval_command": {
                "expected": ["python", "fit.py", "--config", str(cfg_old)],
                "command": ["python", "fit.py", "--config", str(cfg_new)],
            }}},
        }) + "\n",
        encoding="utf-8",
    )
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "fit1",
                                  "idea": {"operator": "fit1", "params": {"steps": 977}},
                                  "code": "", "files": {"candidate-notes/n1.txt": "new"}})
    store.append("effective_config", {
        "node_id": 1, "generation": 0, "version": 1,
        "effective": {"max_steps": 977.0, "world_size": 4.0},
        "by_stage": {}, "conflicts": [], "declared": {"steps": 977.0},
        "mismatches": [], "unknown": [], "checked": 1, "sources_read": [f"config:{cfg_new}"],
    })

    with TestClient(make_app(root)) as client:
        gen = run_generation_token(store.read_all())
        r0 = client.get("/api/runs/demo/nodes/0/runtime-params",
                        params={"expected_generation": gen, "attempt": 0}).json()
        assert [c["origin"] for c in r0["carriers"]] == [rp.ORIGIN_EVAL_COMMAND]
        assert r0["carriers"][0]["path"] == str(cfg_old)
        kv0 = {p["key"]: p["value"] for p in r0["params"]}
        assert kv0["steps"] == "500" and kv0["api_token"] == "***"
        assert "secret-tok-123456" not in json.dumps(r0)

        r1 = client.get("/api/runs/demo/nodes/1/runtime-params",
                        params={"expected_generation": gen, "attempt": 0}).json()
        assert r1["carriers"][0]["path"] == str(cfg_new)
        assert r1["effective_config"]["effective"]["max_steps"] == 977.0


def test_research_quality_job_roles_and_research_map_reverse_links(tmp_path, monkeypatch):
    state = SimpleNamespace(
        run_id="r",
        research=[],
        nodes={
            1: SimpleNamespace(id=1, job_role="profile", idea=SimpleNamespace(parent_card_id=None)),
            2: SimpleNamespace(id=2, job_role="fit", idea=SimpleNamespace(parent_card_id="c-dir")),
            3: SimpleNamespace(id=3, job_role=None, idea=SimpleNamespace(parent_card_id=None)),
        },
        report={},
    )
    q = research_quality(state, {})
    assert q["job_role_counts"] == {"profile": 1, "fit": 1}
    assert q["engineering_nodes_excluded"] == 1
    assert q["unclassified_nodes"] == 1  # only node 3 (job_role=None) stays unclassified

    monkeypatch.setenv("LOOPLAB_UI_ANONYMOUS", "1")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    root = tmp_path / "runs"
    rd = root / "r"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True).model_dump_json(), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "run_uid": "uid-r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("hypothesis_added", {"id": "c-dir", "card_kind": "direction", "statement": "Cut history"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "score_cut", "node_kind": "artifact", "job_role": "score",
        "idea": {"operator": "score_cut", "parent_card_id": "c-dir", "hypothesis": "test"},
    })
    store.append("node_evaluated", {"node_id": 0, "metric": None, "extra_metrics": {"recall@200": 0.42}})
    memo_id = "memo:sha256:" + "a" * 64
    store.append("research_completed", {
        "memo": {"memo_id": memo_id, "at_node": 0, "summary": "s", "claims": [{"statement": "c", "node_ids": [0]}]}
    })
    gen = run_generation_token(store.read_all())
    (rd / "protocol_amendments.jsonl").write_text(json.dumps({
        "amendment_id": "am1", "version": 1, "kind": "experiment", "run_uid": "uid-r", "generation": gen,
        "at_event_seq": 0, "payload": {
            "experiment_id": "exp_cut", "title": "Cut", "question_card_id": "c-dir",
            "arms": {"A": {"node_id": 0, "work_kind": "score_only"}},
            "tests": [{"label": "t1"}], "metrics": [{"name": "recall@200", "kind": "mean"}], "contrasts": [],
        },
    }) + "\n", encoding="utf-8")

    npz_payload = write_npz({
        "keys": write_npy(["q1", "q2"], dtype="<U4"),
        "users": write_npy(["u1", "u2"], dtype="<U4"),
        "vals": write_npy([0.0, 1.0], shape=(2,), dtype="<f8"),
    })
    with TestClient(make_app(root)) as client:
        up = client.post("/api/runs/r/per-query", headers={"X-LoopLab-Token": "agent-secret"}, json={
            "expected_generation": gen, "action_id": "u1", "node_id": 0, "format": "npz",
            "content_b64": base64.b64encode(npz_payload).decode(),
            "metrics": {"recall@200": {"kind": "mean", "array": "vals"}},
            "test_label": "t1", "experiment_id": "exp_cut", "arm": "A",
        })
        assert up.status_code == 200, up.text
        gen2 = run_generation_token(store.read_all())
        m = client.get("/api/runs/r/research-map", params={"expected_generation": gen2}).json()
        card = next(c for c in m["cards"]["items"] if c["card_id"] == "c-dir")
        assert card["linked_experiment_ids"] == ["exp_cut"]
        assert card["linked_node_ids"] == [0]
        (node,) = m["nodes"]["items"]
        assert node["experiment_arms"] == [{"experiment_id": "exp_cut", "arm": "A"}]
        assert node["citing_memo_ids"] == [memo_id]
        assert len(node["upload_ids"]) == 1
        (upload,) = m["uploads"]["items"]
        assert upload["scalar_applicability"] == "not_applicable_artifact"
