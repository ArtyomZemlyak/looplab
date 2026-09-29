"""The runs inside CAMPAIGN folders — `looplab run --out runs/<campaign>/<seed>` (doc 70 70.1).

`/api/runs` lists the root's children only, so a campaign's runs were invisible in the UI. They are
listed by `GET /api/campaign-runs`, grouped by folder, as the run list's own rows — and READ-ONLY: a
nested run is not addressable by the per-run routes (one path segment, `AppState.run_dir`'s
direct-child rule), which is exactly why they are NOT rows of `/api/runs`, where every caller opens
what it lists. The one property this must never trade away is that rule's reason: a run's own node
workspaces are sandbox-writable, so a directory INSIDE a run is never read as a run.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import run_projections  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _run(rd, *, metric=1.0, task="t"):
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": rd.name, "task_id": task, "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": metric,
                                    "violations": []})
    return rd


def _client(root):
    return TestClient(make_app(root))


def test_a_campaign_s_runs_are_listed_by_folder_and_not_as_runs(tmp_path):
    root = tmp_path / "runs"
    _run(root / "solo")
    _run(root / "campA" / "seed1", metric=0.5)
    _run(root / "campA" / "seed2", metric=0.25)
    _run(root / "campB" / "run", metric=2.0)
    client = _client(root)
    listed = client.get("/api/campaign-runs")
    assert listed.status_code == 200, listed.text
    view = listed.json()
    assert [f["folder"] for f in view["folders"]] == ["campA", "campB"]
    camp_a = view["folders"][0]
    assert camp_a["run_root"] == str(root / "campA")
    assert [(r["run_id"], r["best_metric"]) for r in camp_a["runs"]] == [("seed1", 0.5),
                                                                          ("seed2", 0.25)]
    assert all(r["engine_running"] is False for r in camp_a["runs"])
    assert view["folders_skipped"] == 0 and camp_a["runs_skipped"] == 0
    # The run list's own row, keyed apart in the fold cache (`campA/seed1`), so two campaigns'
    # `run` (the benchmark scripts' leaf name) never share a row.
    solo = client.get("/api/runs").json()
    assert [r["run_id"] for r in solo] == ["solo"], "a nested run is not a row every caller opens"
    assert set(camp_a["runs"][0]) >= set(solo[0]) - {"project_id", "label", "supertask_id"}


def test_nothing_nested_becomes_addressable(tmp_path):
    """Listing is not addressing: the per-run routes keep the direct-child rule for every spelling."""
    root = tmp_path / "runs"
    _run(root / "campA" / "seed1")
    client = _client(root)
    for run_id in ("seed1", "campA", "campA%2Fseed1", "campA:seed1", "campA~seed1"):
        assert client.get(f"/api/runs/{run_id}/state").status_code == 404, run_id


def test_a_directory_inside_a_run_is_never_a_campaign(tmp_path):
    """THE SECURITY PROPERTY: a run's node workspaces are sandbox-writable, so a candidate can plant
    an `events.jsonl` there. A folder that IS a run (its log) or is one in setup (any other marker)
    is never descended. MUTATION: drop the run/marker clause of `campaign_folder` -> the planted
    logs are listed."""
    root = tmp_path / "runs"
    run = _run(root / "demo")
    _run(run / "planted")                                   # a candidate-written "run", one down
    setup = root / "in_setup"
    setup.mkdir(parents=True)
    (setup / "config.snapshot.json").write_text("{}")
    _run(setup / "nodes")                                   # under a run still in setup
    view = _client(root).get("/api/campaign-runs").json()
    assert view["folders"] == []


@pytest.mark.parametrize("name", [".hidden", "reports", "assistant", ".command-locks",
                                  ".looplab-lifecycle-abc"])
def test_the_root_s_own_stores_are_not_campaigns(tmp_path, name):
    root = tmp_path / "runs"
    _run(root / name / "seed1")
    assert _client(root).get("/api/campaign-runs").json()["folders"] == []


@pytest.mark.posix_only("symlink")
def test_a_linked_folder_or_run_is_not_followed(tmp_path):
    root = tmp_path / "runs"
    real = _run(tmp_path / "elsewhere" / "seed1").parent
    root.mkdir()
    os.symlink(real, root / "linked", target_is_directory=True)
    _run(root / "campA" / "seed1")
    os.symlink(tmp_path / "elsewhere" / "seed1", root / "campA" / "alias", target_is_directory=True)
    view = _client(root).get("/api/campaign-runs").json()
    assert [f["folder"] for f in view["folders"]] == ["campA"]
    assert [r["run_id"] for r in view["folders"][0]["runs"]] == ["seed1"]


def test_the_bounds_are_counted_never_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(run_projections, "CAMPAIGN_RUNS_CAP", 1)
    monkeypatch.setattr(run_projections, "CAMPAIGN_FOLDERS_CAP", 1)
    root = tmp_path / "runs"
    _run(root / "campA" / "seed1")
    _run(root / "campA" / "seed2")
    _run(root / "campB" / "seed1")
    view = _client(root).get("/api/campaign-runs").json()
    assert [f["folder"] for f in view["folders"]] == ["campA"]
    assert view["folders"][0]["runs_skipped"] == 1 and view["folders_skipped"] == 1


def test_the_list_is_a_read(tmp_path):
    """No engine is reconciled or spawned for a campaign run, and nothing under the root changes."""
    root = tmp_path / "runs"
    run = _run(root / "campA" / "seed1")
    (run / "config.snapshot.json").write_text("{}")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    client = _client(root)
    assert client.get("/api/campaign-runs").status_code == 200
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert after == before
