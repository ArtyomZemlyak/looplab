"""A research campaign inside ONE run (doc 79): pre-registered experiments, recorded amendments of
the task / base files / budget, per-query uploads, LoopLab-computed paired comparisons, shared-test
leaderboards across runs and the super-task campaign report.

Driven through the real app with the real auth middleware: the agent token and the owner are the
two authors every record must name."""
from __future__ import annotations

import base64
import json
import math
import random

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.core.npyio import write_npy, write_npz
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app
from looplab.trust import paired

AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "owner-secret"}
N_USERS = 300


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("lr = 1e-3\nprint('train')\n")
    (repo / "exp.env").write_text("ARM=A\n")
    return repo


def _make_run(root, run_id, repo, *, nodes=(0, 1, 2), metrics=None):
    rd = root / run_id
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(
        Settings(backend="toy", external_harness=True).model_dump_json())
    task = {"kind": "repo", "id": run_id, "direction": "max", "goal": "g",
            "editable_path": str(repo), "edit_surface": ["exp.env"], "protect": ["train.py"],
            "eval": {"command": ["python", "score.py"],
                     "metric": {"kind": "stdout_json", "key": "recall"}, "inputs": []}}
    (rd / "task.snapshot.json").write_text(json.dumps(task, indent=2))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "run_uid": f"uid-{run_id}",
                                 "task_id": run_id, "goal": "g", "direction": "max"})
    for nid in nodes:
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "theme": f"arm{nid}"},
                                      "files": {"exp.env": f"ARM={nid}\n"}})
        if metrics is not None:
            store.append("node_evaluated", {"node_id": nid, "metric": metrics[nid]})
    return rd, store


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    root = tmp_path / "runs"
    root.mkdir()
    client = TestClient(make_app(root))
    # No engine is running for these runs; liveness is proven-free.
    monkeypatch.setattr(client.app.state.looplab.commands, "engine_liveness", lambda _rd: False)
    return root, client


def _arrays(seed, lift=0.0):
    """Per-query arrays shaped like the MiniOneRec analysis npz: 2 queries per user, 3 buckets."""
    rng = random.Random(seed)
    keys, users, hits, den, weighted = [], [], [], [], []
    for u in range(N_USERS):
        for q in range(2):
            keys.append(f"q{u}-{q}")
            users.append(f"u{u}")
            d = [rng.randint(0, 2) for _ in range(3)]
            h = [min(x, 1 if rng.random() < 0.3 + lift + 0.05 * (u % 3) else 0) for x in d]
            den.extend(d)
            hits.extend(h)
            total = sum(d)
            weighted.extend([(hh / total) if total else 0.0 for hh in h])
    payload = write_npz({
        "keys": write_npy(keys, dtype="<U12"), "users": write_npy(users, dtype="<U8"),
        "den": write_npy(den, shape=(len(keys), 3), dtype="<i4"),
        "hits200": write_npy(hits, shape=(len(keys), 3), dtype="<i4"),
        "weighted200": write_npy(weighted, shape=(len(keys), 3), dtype="<f8"),
    })
    per_query = [sum(weighted[i * 3:(i + 1) * 3]) for i in range(len(keys))]
    return payload, per_query, users, hits, den


METRICS = {"FUR@200": {"kind": "mean", "array": "weighted200"},
           "rare@200": {"kind": "ratio", "num": "hits200", "den": "den", "columns": [0, 1]}}


def _gen(store):
    return run_generation_token(store.read_all())


def _upload(client, run_id, store, node_id, payload, *, headers=AGENT, action=None, **extra):
    body = {"expected_generation": _gen(store), "action_id": action or f"up-{run_id}-{node_id}",
            "node_id": node_id, "format": "npz",
            "content_b64": base64.b64encode(payload).decode(), "metrics": METRICS,
            "test_label": "original_ic", **extra}
    return client.post(f"/api/runs/{run_id}/per-query", json=body, headers=headers)


def _experiment(**over):
    exp = {"experiment_id": "D4", "title": "repeat vs fresh",
           "question": "Does repeating data help at equal updates?",
           "hypothesis": "A2 beats A0 on in-cart recall@200",
           "arms": {"A0": {"description": "baseline"}, "A1": {}, "A2": {}},
           "tests": [{"label": "original_ic"}],
           "metrics": [{"name": "FUR@200", "kind": "mean"}, {"name": "rare@200", "kind": "ratio"}],
           "contrasts": [{"treatment": "A2", "control": "A0"},
                         {"treatment": "A1", "control": "A0"},
                         {"treatment": "A2", "control": "A0", "metric": "rare@200",
                          "family": "secondary"}]}
    exp.update(over)
    return exp


def _amend(client, run_id, store, kind, *, headers=AGENT, action=None, reason="pre-registered plan", **payload):
    body = {"expected_generation": _gen(store), "action_id": action or f"{kind}-1", "kind": kind,
            "reason": reason, **payload}
    return client.post(f"/api/runs/{run_id}/protocol/amendments", json=body, headers=headers)


def test_campaign_in_one_run_end_to_end(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    rd, store = _make_run(root, "camp", repo, nodes=())
    # Pre-register BEFORE any result exists.
    r = _amend(client, "camp", store, "experiment", experiment=_experiment())
    assert r.status_code == 200, r.text
    reg = r.json()["amendment"]
    assert reg["author"] == {"kind": "agent", "credential": "agent_token"}
    assert reg["version"] == 1 and reg["kind"] == "experiment"
    # Exact retry replays; same action with another body is a conflict; an id is immutable.
    again = _amend(client, "camp", store, "experiment", experiment=_experiment())
    assert again.json()["replayed"] is True
    other = _amend(client, "camp", store, "experiment", experiment=_experiment(title="x"))
    assert other.status_code == 409
    dup = _amend(client, "camp", store, "experiment", action="experiment-2",
                 experiment=_experiment(title="x"))
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "experiment_exists"

    # Results arrive later (three nodes measured by "LoopLab").
    datasets = {nid: _arrays(nid, lift=0.04 * nid) for nid in (0, 1, 2)}
    for nid in (0, 1, 2):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "files": {"exp.env": "A\n"}})
        store.append("node_evaluated", {"node_id": nid,
                                        "metric": sum(datasets[nid][1]) / len(datasets[nid][1])})
    for nid, arm in ((0, "A0"), (1, "A1"), (2, "A2")):
        r = _upload(client, "camp", store, nid, datasets[nid][0], experiment_id="D4", arm=arm,
                    measured_check={"metric": "FUR@200"})
        assert r.status_code == 200, r.text
        up = r.json()["upload"]
        assert up["consistency"]["status"] == "matches_measured"
        assert up["test_id"].startswith("qs1-") and up["n_clusters"] == N_USERS
        assert up["source"]["origin"] == "agent_upload"
    # Same test id for all three (same queries and users).
    view = client.get("/api/runs/camp/per-query", headers=OWNER).json()
    assert len(view["tests"]) == 1 and view["tests"][0]["nodes"] == 3
    # An arm is one node.
    clash = _upload(client, "camp", store, 1, datasets[1][0], action="clash",
                    experiment_id="D4", arm="A2")
    assert clash.status_code == 409

    comp = client.get("/api/runs/camp/experiments/D4/comparison", headers=AGENT).json()
    rows = {(c["treatment"], c["control"], c["metric"]): c for c in comp["contrasts"]}
    main = rows[("A2", "A0", "FUR@200")]
    assert main["status"] == "computed" and main["registered_before_result"] is True
    expect = paired.paired_mean_delta(datasets[2][1], datasets[0][1], datasets[0][2])
    assert math.isclose(main["delta"], expect["estimate"], rel_tol=1e-12)
    assert math.isclose(main["se"], expect["se"], rel_tol=1e-9)
    assert main["clusters"] == N_USERS and main["n"] == 2 * N_USERS
    # Holm over the PRIMARY family only (two contrasts); the secondary is its own family of one.
    primary = [rows[("A2", "A0", "FUR@200")], rows[("A1", "A0", "FUR@200")]]
    assert [c["p_adjusted"] for c in primary] == paired.holm([c["p_two_sided"] for c in primary])
    assert all(c["family_size"] == 2 for c in primary)
    rare = rows[("A2", "A0", "rare@200")]
    assert rare["family_size"] == 1 and rare["p_adjusted"] == rare["p_two_sided"]
    _p, _q, users, hits2, den2 = datasets[2]
    _p, _q, _u, hits0, den0 = datasets[0]
    pick = lambda arr: [arr[i * 3] + arr[i * 3 + 1] for i in range(len(users))]
    expect = paired.paired_ratio_delta(pick(hits2), pick(den2), pick(hits0), pick(den0), users)
    assert math.isclose(rare["delta"], expect["estimate"], rel_tol=1e-12)
    assert main["treatment_source"]["source_sha256"] and comp["computed_by"]["engine"] == "looplab"
    # A bootstrap interval is offered on request and brackets the estimate.
    boot = client.get("/api/runs/camp/experiments/D4/comparison",
                      params={"bootstrap": 200}, headers=OWNER).json()
    b = {(c["treatment"], c["metric"]): c for c in boot["contrasts"]}[("A2", "FUR@200")]["bootstrap"]
    assert b["ci95_low"] < main["delta"] < b["ci95_high"]

    # A post-hoc experiment is flagged as such.
    r = _amend(client, "camp", store, "experiment", action="posthoc",
               experiment=_experiment(experiment_id="D4b", supersedes="D4",
                                      arms={"A0": {"node_id": 0}, "A2": {"node_id": 2}},
                                      contrasts=[{"treatment": "A2", "control": "A0"}]))
    assert r.status_code == 200, r.text
    late = client.get("/api/runs/camp/experiments/D4b/comparison", headers=OWNER).json()
    assert late["contrasts"][0]["registered_before_result"] is False
    assert any("post hoc" in c for c in late["caveats"])
    proto = client.get("/api/runs/camp/protocol", headers=OWNER).json()
    assert proto["protocol_version"] == 2
    assert [e["experiment_id"] for e in proto["experiments"]] == ["D4", "D4b"]
    assert proto["experiments"][0]["superseded"] is True

    # Reset a node: its upload becomes stale and says so.
    store.append("node_reset", {"node_id": 2, "generation": 0, "from_stage": "eval"})
    stale = client.get("/api/runs/camp/experiments/D4/comparison", headers=OWNER).json()
    row = {(c["treatment"], c["metric"]): c for c in stale["contrasts"]}[("A2", "FUR@200")]
    assert row["treatment_source"]["stale"] is True


def test_cross_run_arm_and_shared_test_leaderboard_and_campaign_report(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    data = {nid: _arrays(nid, lift=0.03 * nid) for nid in (0, 1)}
    means = {nid: sum(d[1]) / len(d[1]) for nid, d in data.items()}
    rd_a, store_a = _make_run(root, "run-a", repo, nodes=(0,), metrics=means)
    rd_b, store_b = _make_run(root, "run-b", repo, nodes=(1,), metrics=means)
    assert _upload(client, "run-a", store_a, 0, data[0][0]).status_code == 200
    # The experiment lives in run-b and reads its baseline from run-a.
    exp = _experiment(experiment_id="X", arms={"base": {"run_id": "run-a", "node_id": 0},
                                               "new": {}},
                      contrasts=[{"treatment": "new", "control": "base"}])
    assert _amend(client, "run-b", store_b, "experiment", experiment=exp).status_code == 200
    assert _amend(client, "run-b", store_b, "experiment", action="bad",
                  experiment=_experiment(experiment_id="Y", arms={"b": {"run_id": "nope"}, "c": {}},
                                         contrasts=[])).status_code == 400
    r = _upload(client, "run-b", store_b, 1, data[1][0], experiment_id="X", arm="new")
    assert r.status_code == 200, r.text
    test_id = r.json()["upload"]["test_id"]
    comp = client.get("/api/runs/run-b/experiments/X/comparison", headers=OWNER).json()
    row = comp["contrasts"][0]
    assert row["status"] == "computed"
    assert row["control_source"]["run_id"] == "run-a" and row["treatment_source"]["run_id"] == "run-b"
    board = client.get(f"/api/shared-tests/{test_id}", params={"metric": "FUR@200",
                                                                "reference": "run-a:0"},
                       headers=AGENT).json()
    assert [e["run_id"] for e in board["entries"]] == ["run-b", "run-a"]
    assert math.isclose(board["entries"][0]["vs_reference"]["estimate"], row["delta"], rel_tol=1e-12)

    # The agent groups both runs under a super-task it creates, and authors the narrative.
    st = client.post("/api/supertasks", json={"name": "data scaling"}, headers=AGENT).json()
    runs = client.get("/api/runs", headers=AGENT).json()
    rows = runs["runs"] if isinstance(runs, dict) else runs
    gens = {r["run_id"]: r for r in rows}
    for rid in ("run-a", "run-b"):
        gen = gens[rid].get("generation")
        resp = client.post(f"/api/runs/{rid}/supertask", headers=AGENT, json={
            "supertask_id": st["id"], "expected_supertask_id": None,
            "expected_generation": gen})
        assert resp.status_code == 200, resp.text
    report = client.get(f"/api/supertasks/{st['id']}/campaign", headers=OWNER).json()
    assert [r["run_id"] for r in report["runs"]] == ["run-a", "run-b"]
    assert report["leaderboards"][0]["test_id"] == test_id
    assert report["runs"][1]["experiments"][0]["contrasts"][0]["status"] == "computed"
    assert report["narrative"] is None
    body = {"action_id": "n1", "expected_revision": 0, "title": "Data scaling",
            "body_markdown": "## Finding\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
            "key_findings": ["new > base"]}
    first = client.post(f"/api/supertasks/{st['id']}/campaign/narrative", json=body, headers=AGENT)
    assert first.status_code == 200, first.text
    assert first.json()["narrative"]["author"]["kind"] == "agent"
    stale = client.post(f"/api/supertasks/{st['id']}/campaign/narrative",
                        json={**body, "action_id": "n2"}, headers=OWNER)
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1
    report = client.get(f"/api/supertasks/{st['id']}/campaign", headers=OWNER).json()
    assert report["narrative"]["revision"] == 1


def test_task_and_file_amendments_need_a_stopped_engine_and_record_diffs(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    rd, store = _make_run(root, "camp", repo, nodes=(0,), metrics={0: 0.5})
    new_input = tmp_path / "new.csv"
    new_input.write_text("a\n")
    task_change = {"inputs_add": [str(new_input)], "protect_add": ["data.py"]}
    commands = client.app.state.looplab.commands
    monkeypatch.setattr(commands, "engine_liveness", lambda _rd: True)
    live = _amend(client, "camp", store, "task", task=task_change)
    assert live.status_code == 409 and live.json()["detail"]["code"] == "engine_running"
    assert json.loads((rd / "task.snapshot.json").read_text())["eval"]["inputs"] == []
    monkeypatch.setattr(commands, "engine_liveness", lambda _rd: False)
    r = _amend(client, "camp", store, "task", task=task_change, reason="new dataset for D3")
    assert r.status_code == 200, r.text
    payload = r.json()["amendment"]["payload"]
    assert payload["before_identity"] != payload["after_identity"]
    assert str(new_input) in payload["diff"]
    snap = json.loads((rd / "task.snapshot.json").read_text())
    assert str(new_input) in snap["eval"]["inputs"] and "data.py" in snap["protect"]

    import hashlib
    old = (repo / "train.py").read_bytes()
    fix = {"path": "train.py", "content": "lr = 2e-4\nprint('train')\n",
           "expected_sha256": hashlib.sha256(old).hexdigest()}
    r = _amend(client, "camp", store, "files", files=[fix], reason="fix max_steps bug in trainer")
    assert r.status_code == 200, r.text
    receipt = r.json()["amendment"]["payload"]["files"][0]
    assert receipt["protected"] is True and "-lr = 1e-3" in receipt["diff"]
    assert (repo / "train.py").read_text().startswith("lr = 2e-4")
    # A stale CAS, an edit-surface file and a path outside the root are refused.
    assert _amend(client, "camp", store, "files", action="f2", files=[fix]).status_code == 409
    surface = {"path": "exp.env", "content": "X\n", "expected_sha256": None}
    assert _amend(client, "camp", store, "files", action="f3", files=[surface]).status_code == 400
    outside = {"path": "../x.py", "content": "X\n", "expected_sha256": None}
    assert _amend(client, "camp", store, "files", action="f4", files=[outside]).status_code == 400
    trail = client.get("/api/runs/camp/protocol", headers=OWNER).json()["amendments"]
    assert [a["kind"] for a in trail] == ["task", "files"]
    assert all(a["author"]["kind"] == "agent" and a["reason"] for a in trail)

    # The operator narrows the policy; the agent may no longer touch files (the owner still may).
    assert client.put("/api/runs/camp/harness-policy", json={"amendments": "inputs"},
                      headers=AGENT).status_code == 403
    assert client.put("/api/runs/camp/harness-policy", json={"amendments": "inputs"},
                      headers=OWNER).status_code == 200
    cur = hashlib.sha256((repo / "train.py").read_bytes()).hexdigest()
    again = {"path": "train.py", "content": "lr = 3e-4\n", "expected_sha256": cur}
    refused = _amend(client, "camp", store, "files", action="f5", files=[again])
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "amendment_not_permitted"
    owner = _amend(client, "camp", store, "files", action="f6", files=[again], headers=OWNER)
    assert owner.status_code == 200 and owner.json()["amendment"]["author"]["kind"] == "operator"


def test_amendment_rolls_back_when_its_record_cannot_be_written(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    rd, store = _make_run(root, "camp", repo, nodes=())
    before = (rd / "task.snapshot.json").read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr("looplab.harness.campaign.append_jsonl_bytes_locked", boom)
    r = _amend(client, "camp", store, "task", task={"protect_add": ["x.py"]})
    assert r.status_code == 503
    assert (rd / "task.snapshot.json").read_bytes() == before


def test_agent_budget_extension_is_capped_by_the_operator(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    rd, store = _make_run(root, "camp", repo, nodes=(0,), metrics={0: 0.1})
    calls = []

    def fake_submit(rd_, key, event_type, data, *, expected_generation=None, agent_token=False, **_k):
        from looplab.serve.control_validation import normalize_control
        normalize_control(client.app.state.looplab, rd_, event_type, data, agent_token=agent_token)
        calls.append((key, event_type, data, agent_token))
        record = {"id": "cmd_" + "0" * 32, "status": "succeeded", "event_type": event_type,
                  "data": data, "submitted_by": "agent_token" if agent_token else None}
        (rd / ".commands").mkdir(exist_ok=True)
        (rd / ".commands" / f"cmd_{len(calls):032d}.json").write_text(json.dumps(record))
        return record
    monkeypatch.setattr(client.app.state.looplab.commands, "submit", fake_submit)
    client.put("/api/runs/camp/harness-policy", json={"max_agent_added_nodes": 3}, headers=OWNER)
    ok = _amend(client, "camp", store, "budget", budget={"add_nodes": 2},
                reason="G retry needs one more node slot")
    assert ok.status_code == 200, ok.text
    assert ok.json()["amendment"]["payload"]["add_nodes"] == 2
    assert calls[0][1] == "budget_extend" and calls[0][3] is True
    over = _amend(client, "camp", store, "budget", action="budget-2", budget={"add_nodes": 2})
    assert over.status_code == 403 and over.json()["detail"]["code"] == "agent_budget_cap"
    policy = client.get("/api/runs/camp/harness-policy", headers=AGENT).json()
    assert policy["agent_added_nodes"] == 2 and policy["max_agent_added_nodes"] == 3


def test_old_runs_read_an_empty_protocol(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "old", _repo(tmp_path), nodes=(0,), metrics={0: 1.0})
    proto = client.get("/api/runs/old/protocol", headers=OWNER).json()
    assert proto["experiments"] == [] and proto["amendments"] == []
    assert proto["policy"]["source"] == "default"
    assert client.get("/api/runs/old/per-query", headers=OWNER).json()["uploads"] == []
    assert client.get("/api/runs/old/experiments/none/comparison", headers=OWNER).status_code == 404


@pytest.mark.parametrize("bad", [
    {"experiment_id": "has space"},
    {"arms": {}},
    {"contrasts": [{"treatment": "A0", "control": "A0"}]},
    {"metrics": [{"name": "m", "kind": "median"}]},
    {"tests": [{"label": "t", "test_id": "not-an-id"}]},
])
def test_experiment_declarations_are_validated(tmp_path, monkeypatch, bad):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    assert _amend(client, "camp", store, "experiment",
                  experiment=_experiment(**bad)).status_code == 400
