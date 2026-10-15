"""Each node is bound, IN THE FOLD, to the campaign protocol version it was created under (doc 81 §8
leftover, 2026-10-14): the engine writes `protocol_version` on `node_created` from the run's
amendment journal, the fold reads it into `Node.protocol_version`, and a comparison whose arms were
created under different versions says so."""
from __future__ import annotations

import json
from types import SimpleNamespace

import anyio

from looplab.engine.node_build import _protocol_version_now
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from factories import make_engine
from test_campaign_protocol import (_amend, _arrays, _client, _experiment, _make_run, _repo,
                                    _upload, OWNER)


def _ev(type_, **data):
    return SimpleNamespace(type=type_, data=data, seq=0)


def test_the_version_counts_this_incarnations_journal_rows(tmp_path):
    engine = SimpleNamespace(run_dir=tmp_path, store=SimpleNamespace(
        read_all=lambda: [_ev("run_started", run_uid="u1")]))
    assert _protocol_version_now(engine) is None                  # no journal: no key at all
    rows = [{"run_uid": "u1", "version": 1}, {"run_uid": "old", "version": 1},
            {"run_uid": "u1", "version": 2}]
    (tmp_path / "protocol_amendments.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + "{torn")
    assert _protocol_version_now(engine) == 2


def test_the_fold_binds_the_version_and_old_rows_stay_unbound(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    for nid, version in ((0, None), (1, 0), (2, 3)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "code": "", "files": {},
                                      **({"protocol_version": version} if version is not None
                                         else {})})
    store.append("node_created", {"node_id": 3, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "", "files": {},
                                  "protocol_version": "x"})
    nodes = fold(store.read_all()).nodes
    assert [nodes[i].protocol_version for i in range(4)] == [None, 0, 3, None]


def test_the_engine_writes_the_version_only_when_a_journal_exists(tmp_path):
    rd = tmp_path / "run"
    anyio.run(make_engine(rd, n_seeds=1, max_nodes=1).run)
    created = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_created"]
    assert created and all("protocol_version" not in e.data for e in created)
    rd2 = tmp_path / "run2"
    rd2.mkdir()
    (rd2 / "protocol_amendments.jsonl").write_text(json.dumps({"run_uid": "elsewhere"}) + "\n")
    anyio.run(make_engine(rd2, n_seeds=1, max_nodes=1).run)
    created = [e for e in EventStore(rd2 / "events.jsonl").read_all() if e.type == "node_created"]
    assert created and all(e.data.get("protocol_version") == 0 for e in created)
    assert fold(EventStore(rd2 / "events.jsonl").read_all()).nodes[0].protocol_version == 0


def test_a_comparison_across_protocol_versions_is_a_caveat(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    assert _amend(client, "camp", store, "experiment", experiment=_experiment()).status_code == 200
    # Version 2 is a TASK amendment — the kind of row that changes the protocol (an experiment
    # registration or a budget extension also bumps the count but changes nothing the arms ran).
    assert _amend(client, "camp", store, "task", action="t1",
                  task={"protect_add": ["x.py"]}).status_code == 200
    datasets = {nid: _arrays(nid, lift=0.04 * nid) for nid in (0, 1, 2)}
    for nid, version in ((0, 1), (1, 1), (2, 2)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "files": {"exp.env": "A\n"},
                                      "protocol_version": version})
        store.append("node_evaluated", {"node_id": nid,
                                        "metric": sum(datasets[nid][1]) / len(datasets[nid][1])})
    for nid, arm in ((0, "A0"), (1, "A1"), (2, "A2")):
        assert _upload(client, "camp", store, nid, datasets[nid][0], experiment_id="D4",
                       arm=arm).status_code == 200
    comp = client.get("/api/runs/camp/experiments/D4/comparison", headers=OWNER).json()
    rows = {(c["treatment"], c["metric"]): c for c in comp["contrasts"]}
    assert rows[("A1", "FUR@200")]["same_protocol"] is True
    assert rows[("A2", "FUR@200")]["same_protocol"] is False
    assert (rows[("A2", "FUR@200")]["treatment_protocol_version"],
            rows[("A2", "FUR@200")]["control_protocol_version"]) == (2, 1)
    assert any("DIFFERENT protocol versions" in c for c in comp["caveats"])
