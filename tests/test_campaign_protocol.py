"""A research campaign inside ONE run (doc 81): pre-registered experiments, recorded amendments of
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


def test_the_policy_level_binds_every_agent_budget_field(tmp_path, monkeypatch):
    """Review 2026-10-15: the policy level was checked only when `add_nodes` was sent, so under
    `amendments: off` the agent token could still extend the wall-clock and eval budgets. The level
    now binds every budget-EXTENDING field; widths and the operator are untouched."""
    from fastapi import HTTPException

    from looplab.serve.control_validation import normalize_control
    root, client = _client(tmp_path, monkeypatch)
    rd, _store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.1})
    srv = client.app.state.looplab
    assert client.put("/api/runs/camp/harness-policy", json={"amendments": "off"},
                      headers=OWNER).status_code == 200
    for field, value in (("max_seconds", 3600), ("max_eval_seconds", 3600), ("timeout", 600),
                         ("eval_timeout", 600), ("add_nodes", 1)):
        with pytest.raises(HTTPException) as info:
            normalize_control(srv, rd, "budget_extend", {field: value}, agent_token=True)
        assert info.value.status_code == 403, field
        assert info.value.detail["code"] == "amendment_not_permitted", field
        assert normalize_control(srv, rd, "budget_extend", {field: value})[field] == value
    assert normalize_control(srv, rd, "budget_extend", {"eval_parallel": 2},
                             agent_token=True)["eval_parallel"] == 2


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


# ----------------------------------------------------------------- review 2026-10-15 (g6 findings)
def test_an_upload_with_one_cluster_is_refused_before_it_is_written(tmp_path, monkeypatch):
    """A clustered SE over ONE user is NaN; stored, it made the upload's own response and every
    later comparison over the test unrenderable (Starlette renders JSON with allow_nan=False)."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    payload = write_npz({"keys": write_npy(["q0", "q1", "q2"], dtype="<U4"),
                         "users": write_npy(["u", "u", "u"], dtype="<U2"),
                         "v": write_npy([1.0, 0.0, 1.0], dtype="<f8")})
    body = {"expected_generation": _gen(store), "action_id": "one", "node_id": 0, "format": "npz",
            "content_b64": base64.b64encode(payload).decode(), "metrics": {"m": {"array": "v"}}}
    r = client.post("/api/runs/camp/per-query", json=body, headers=AGENT)
    assert r.status_code == 400 and "clusters" in r.text
    assert not (rd / "per_query").exists() or not any((rd / "per_query").rglob("*.json.gz"))


def test_holm_counts_the_declared_family_not_the_computed_subset(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    assert _amend(client, "camp", store, "experiment", experiment=_experiment()).status_code == 200
    datasets = {nid: _arrays(nid, lift=0.04 * nid) for nid in (0, 2)}
    for nid, arm in ((0, "A0"), (2, "A2")):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "files": {"exp.env": "A\n"}})
        store.append("node_evaluated", {"node_id": nid, "metric": 0.5})
        assert _upload(client, "camp", store, nid, datasets[nid][0], experiment_id="D4",
                       arm=arm).status_code == 200
    comp = client.get("/api/runs/camp/experiments/D4/comparison", headers=OWNER).json()
    rows = {(c["treatment"], c["metric"]): c for c in comp["contrasts"]}
    computed = rows[("A2", "FUR@200")]
    assert rows[("A1", "FUR@200")]["status"] == "unavailable"     # A1 never uploaded
    # The primary family declares two contrasts: the missing one enters as p = 1.
    assert computed["family_size"] == 2 and computed["family_computed"] == 1
    assert computed["p_adjusted"] == paired.holm([computed["p_two_sided"], 1.0])[0]
    assert any("families are incomplete" in c for c in comp["caveats"])


def test_same_protocol_reads_only_task_and_files_amendments(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    exp = _experiment(arms={"A0": {"node_id": 0}, "A2": {"node_id": 2}},
                      contrasts=[{"treatment": "A2", "control": "A0"}])
    assert _amend(client, "camp", store, "experiment", experiment=exp).status_code == 200
    datasets = {nid: _arrays(nid) for nid in (0, 2)}
    # Node 0 under version 1, node 2 under version 2 — the step between them is an EXPERIMENT
    # registration, not a task/files amendment.
    for nid, version in ((0, 1), (2, 2)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "files": {"exp.env": "A\n"},
                                      "protocol_version": version})
        store.append("node_evaluated", {"node_id": nid, "metric": 0.5})
        assert _upload(client, "camp", store, nid, datasets[nid][0]).status_code == 200
        if nid == 0:
            other = _experiment(experiment_id="E2", arms={"A0": {"node_id": 0}, "A1": {}},
                                contrasts=[{"treatment": "A1", "control": "A0"}])
            assert _amend(client, "camp", store, "experiment", action="e2",
                          experiment=other).status_code == 200
    row = client.get("/api/runs/camp/experiments/D4/comparison",
                     headers=OWNER).json()["contrasts"][0]
    assert row["status"] == "computed", row
    assert row["same_protocol"] is True
    # A task amendment between them IS a protocol change.
    assert _amend(client, "camp", store, "task", action="t1",
                  task={"protect_add": ["x.py"]}).status_code == 200
    store.append("node_created", {"node_id": 4, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "files": {"exp.env": "A\n"},
                                  "protocol_version": 3})
    store.append("node_evaluated", {"node_id": 4, "metric": 0.5})
    assert _upload(client, "camp", store, 4, datasets[2][0]).status_code == 200
    exp3 = _experiment(experiment_id="E3", arms={"A0": {"node_id": 0}, "A2": {"node_id": 4}},
                       contrasts=[{"treatment": "A2", "control": "A0"}])
    assert _amend(client, "camp", store, "experiment", action="e3",
                  experiment=exp3).status_code == 200
    late = client.get("/api/runs/camp/experiments/E3/comparison", headers=OWNER).json()
    assert late["contrasts"][0]["same_protocol"] is False


def test_file_amendment_refuses_a_file_the_surface_glob_makes_editable(tmp_path, monkeypatch):
    """`edit_surface` holds globs; the literal membership test let `model.py` under `**/*.py` be
    rewritten in the BASE. A protected file under the same glob stays amendable."""
    import hashlib
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    (repo / "model.py").write_text("x = 1\n")
    rd, store = _make_run(root, "camp", repo, nodes=())
    task = json.loads((rd / "task.snapshot.json").read_text())
    task["edit_surface"] = ["**/*.py"]
    (rd / "task.snapshot.json").write_text(json.dumps(task, indent=2))
    sha = lambda p: hashlib.sha256((repo / p).read_bytes()).hexdigest()
    editable = {"path": "model.py", "content": "x = 2\n", "expected_sha256": sha("model.py")}
    r = _amend(client, "camp", store, "files", action="f1", files=[editable])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "edit_surface_file"
    protected = {"path": "train.py", "content": "lr = 2e-4\n", "expected_sha256": sha("train.py")}
    assert _amend(client, "camp", store, "files", action="f2",
                  files=[protected]).status_code == 200


def test_data_refresh_follows_a_mount_that_is_a_link_to_a_file(tmp_path, monkeypatch):
    from looplab.harness.campaign import content_fingerprint
    target = tmp_path / "v1.parquet"
    target.write_bytes(b"one")
    link = tmp_path / "train.parquet"
    link.symlink_to(target)
    before = content_fingerprint(str(link))["digest"]
    target.write_bytes(b"two")
    assert content_fingerprint(str(link))["digest"] != before


def test_one_mount_refreshed_and_removed_is_a_400_not_a_500(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    data = tmp_path / "train.csv"
    data.write_text("a\n")
    task = json.loads((rd / "task.snapshot.json").read_text())
    task["data"] = {"train": str(data)}
    (rd / "task.snapshot.json").write_text(json.dumps(task, indent=2))
    r = _amend(client, "camp", store, "task", task={"data_refresh": ["train"],
                                                    "data_remove": ["train"]})
    assert r.status_code == 400, r.text


def test_shared_test_leaderboard_ranks_by_direction(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    data = {nid: _arrays(nid, lift=0.03 * nid) for nid in (0, 1)}
    means = {nid: sum(d[1]) / len(d[1]) for nid, d in data.items()}
    _rd, store = _make_run(root, "run-a", repo, nodes=(0, 1), metrics=means)
    for nid in (0, 1):
        r = _upload(client, "run-a", store, nid, data[nid][0])
        assert r.status_code == 200, r.text
    test_id = r.json()["upload"]["test_id"]
    url = f"/api/shared-tests/{test_id}"
    best_first = client.get(url, params={"metric": "FUR@200"}, headers=AGENT).json()
    assert best_first["direction"] == "max" and best_first["direction_source"] == "default"
    assert [e["node_id"] for e in best_first["entries"]] == [1, 0]
    low = client.get(url, params={"metric": "FUR@200", "direction": "min"}, headers=AGENT).json()
    assert [e["node_id"] for e in low["entries"]] == [0, 1]
    assert client.get(url, params={"direction": "up"}, headers=AGENT).status_code == 400
    # An experiment declaring the metric lower-is-better decides the default.
    exp = _experiment(metrics=[{"name": "FUR@200", "kind": "mean", "direction": "min"}],
                      arms={"A0": {"node_id": 0}, "A2": {"node_id": 1}},
                      contrasts=[{"treatment": "A2", "control": "A0"}])
    assert _amend(client, "run-a", store, "experiment", experiment=exp).status_code == 200
    inferred = client.get(url, params={"metric": "FUR@200"}, headers=AGENT).json()
    assert inferred["direction"] == "min" and inferred["direction_source"] == "experiments"
    assert [e["node_id"] for e in inferred["entries"]] == [0, 1]


# ------------------------------------------------- weekly audit 2026-10-15 (OPEN items closed)
def test_a_damaged_journal_row_refuses_the_next_amendment(tmp_path, monkeypatch):
    """weekly-journal-shape-health: a JSON object that fails the shape check may BE an earlier
    amendment; the writer refuses instead of appending version 1 past it."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    (rd / "protocol_amendments.jsonl").write_text(json.dumps({"action_id": "lost"}) + "\n")
    r = _amend(client, "camp", store, "experiment", experiment=_experiment())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "harness_history_incomplete"


def test_a_damaged_manifest_row_refuses_the_next_upload(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    from looplab.harness.per_query import manifest_path, read_manifest
    path = manifest_path(rd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"action_id": "lost"}) + "\n")
    _rows, health = read_manifest(rd)
    assert health["read_complete"] is False and health["invalid_record_rows"] == 1
    r = _upload(client, "camp", store, 0, _arrays(0)[0])
    assert r.status_code == 503, r.text


@pytest.mark.parametrize("queries,clusters", [
    (["a\tb", "z"], ["c", "u"]), (["a", "z"], ["b\tc", "u"]), (["a\nb", "z"], ["c", "u"])])
def test_a_label_holding_a_separator_is_refused(tmp_path, monkeypatch, queries, clusters):
    """weekly-shared-test-delimiters: TAB/newline delimit the test-id serialization, so
    `("a\\tb","c")` and `("a","b\\tc")` were one test id for two different query sets."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    body = {"expected_generation": _gen(store), "action_id": "sep", "node_id": 0, "format": "json",
            "content_b64": base64.b64encode(json.dumps(
                {"keys": queries, "users": clusters, "v": [1.0, 0.0]}).encode()).decode(),
            "metrics": {"m": {"array": "v"}}, "cluster_key": "users"}
    r = client.post("/api/runs/camp/per-query", json=body, headers=AGENT)
    assert r.status_code == 400 and "tab or a newline" in r.text


def test_the_test_id_is_order_free_and_unchanged_for_plain_labels():
    from looplab.harness.per_query import test_id_for
    a = test_id_for(["q1", "q2"], ["u1", "u2"])
    assert a == test_id_for(["q2", "q1"], ["u2", "u1"])
    # The serialization every issued id was minted with (compatibility with stored uploads).
    import hashlib
    assert a == "qs1-" + hashlib.sha256(b"q1\tu1\nq2\tu2").hexdigest()[:32]
    with pytest.raises(ValueError):
        test_id_for(["a\tb"], ["c"])


def test_a_link_to_a_directory_inside_a_mount_is_fingerprinted_by_target(tmp_path):
    """weekly-data-directory-links: retargeting `mount/train` from dataset A to B changed
    nothing in the digest (files=0); a link cycle is still never walked."""
    from looplab.harness.campaign import content_fingerprint
    for name in ("A", "B"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.csv").write_text(name)
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "train").symlink_to(tmp_path / "A", target_is_directory=True)
    (mount / "loop").symlink_to(mount, target_is_directory=True)
    first = content_fingerprint(str(mount))
    assert first["files"] == 2 and not first["truncated"]
    (mount / "train").unlink()
    (mount / "train").symlink_to(tmp_path / "B", target_is_directory=True)
    assert content_fingerprint(str(mount))["digest"] != first["digest"]
