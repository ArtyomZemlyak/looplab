"""ProjectStore CRUD: nesting, cycle-guard, and delete-reassign (the ClearML-style organization
layer for the UI). Pure metadata over projects.json — no engine/event-log involvement."""
from __future__ import annotations

import json

import pytest

from looplab.projects import ProjectError, ProjectStore


def store(tmp_path):
    return ProjectStore(tmp_path / "projects.json")


def test_create_and_persist(tmp_path):
    s = store(tmp_path)
    p = s.create("Vision")
    assert p.parent_id is None and p.name == "Vision"
    # round-trips to disk
    data = json.loads((tmp_path / "projects.json").read_text())
    assert data["projects"][0]["name"] == "Vision"
    assert s.load()["projects"][0]["id"] == p.id


def test_nesting_and_assignment(tmp_path):
    s = store(tmp_path)
    root = s.create("Vision")
    sub = s.create("Detection", parent_id=root.id)
    assert sub.parent_id == root.id
    s.assign("run_a", sub.id)
    assert s.project_of("run_a") == sub.id
    # unassign
    s.assign("run_a", None)
    assert s.project_of("run_a") is None


def test_create_under_unknown_parent_rejected(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ProjectError):
        s.create("Orphan", parent_id="p_missing")


def test_assign_unknown_project_rejected(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ProjectError):
        s.assign("run_a", "p_missing")


def test_reparent_cycle_guard(tmp_path):
    s = store(tmp_path)
    a = s.create("A")
    b = s.create("B", parent_id=a.id)
    c = s.create("C", parent_id=b.id)
    with pytest.raises(ProjectError):
        s.reparent(a.id, c.id)          # a under its own descendant -> cycle
    with pytest.raises(ProjectError):
        s.reparent(a.id, a.id)          # a under itself
    s.reparent(c.id, a.id)              # legal move
    assert s.load()["assignments"] == {}
    assert {p["id"]: p["parent_id"] for p in s.load()["projects"]}[c.id] == a.id


def test_delete_reassigns_children_and_runs(tmp_path):
    s = store(tmp_path)
    root = s.create("Root")
    mid = s.create("Mid", parent_id=root.id)
    leaf = s.create("Leaf", parent_id=mid.id)
    s.assign("run_mid", mid.id)
    s.delete(mid.id)
    idx = {p["id"]: p for p in s.load()["projects"]}
    assert mid.id not in idx
    assert idx[leaf.id]["parent_id"] == root.id      # child reparented to deleted's parent
    assert s.project_of("run_mid") == root.id        # run reassigned to deleted's parent


def test_concurrent_creates_no_lost_update(tmp_path):
    # Each create() is load→mutate→atomic-save; without the lock, threads racing on the same file
    # would clobber each other's writes and drop projects. The barrier maximizes contention.
    import threading
    s = store(tmp_path)
    n = 40
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        s.create(f"p{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.load()["projects"]) == n      # all survive -> no lost update


def test_delete_top_level_unassigns_runs(tmp_path):
    s = store(tmp_path)
    top = s.create("Top")
    s.assign("run_x", top.id)
    s.delete(top.id)
    assert s.project_of("run_x") is None             # no parent -> assignment removed
    assert s.load()["projects"] == []
