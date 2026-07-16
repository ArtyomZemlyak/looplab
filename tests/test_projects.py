"""ProjectStore CRUD: nesting, cycle-guard, and delete-reassign (the ClearML-style organization
layer for the UI). Pure metadata over projects.json — no engine/event-log involvement."""
from __future__ import annotations

import json
import multiprocessing

import pytest

from looplab.serve.projects import ProjectError, ProjectStore, ProjectStoreLockError


def _hold_project_write(path: str, ready, release) -> None:
    project_store = ProjectStore(path)
    with project_store._transaction():
        data = project_store.load()
        data["labels"]["held"] = "first"
        ready.set()
        if not release.wait(15):
            raise TimeoutError("parent did not release project transaction")
        project_store._save(data)


def _write_project_label(path: str, started, done) -> None:
    started.set()
    ProjectStore(path).set_label("writer", "second")
    done.set()


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


def test_cross_process_transaction_waits_then_rereads_without_lost_update(tmp_path):
    path = str(tmp_path / "projects.json")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    started, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_project_write, args=(path, ready, release))
    writer = ctx.Process(target=_write_project_label, args=(path, started, done))
    holder.start()
    try:
        assert ready.wait(10), "holder never acquired the project transaction"
        writer.start()
        assert started.wait(10), "writer process never started its mutation"
        assert not done.wait(0.5), "writer crossed the interprocess lock while it was held"
    finally:
        release.set()
    holder.join(15)
    writer.join(15)
    assert holder.exitcode == writer.exitcode == 0
    assert ProjectStore(path).load()["labels"] == {"held": "first", "writer": "second"}


def test_project_mutation_fails_closed_when_required_lock_is_unavailable(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from looplab.events import eventstore
    from looplab.events.eventstore import EventStoreLockError

    project_store = store(tmp_path)
    project_store.set_label("existing", "keep")
    before = project_store.path.read_bytes()

    @contextmanager
    def unavailable(path, *, required=False):
        assert required is True
        raise EventStoreLockError(path, OSError("locking unsupported"))
        yield  # pragma: no cover - contextmanager syntax only

    monkeypatch.setattr(eventstore, "_interprocess_lock", unavailable)
    with pytest.raises(ProjectStoreLockError, match="project metadata lock is unavailable"):
        project_store.set_label("new", "must-not-land")
    assert project_store.path.read_bytes() == before


def test_project_lock_failure_maps_to_http_503(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from looplab.events import eventstore
    from looplab.events.eventstore import EventStoreLockError
    from looplab.serve.server import make_app

    @contextmanager
    def unavailable(path, *, required=False):
        assert required is True
        raise EventStoreLockError(path, OSError("locking unsupported"))
        yield  # pragma: no cover - contextmanager syntax only

    monkeypatch.setattr(eventstore, "_interprocess_lock", unavailable)
    response = TestClient(make_app(tmp_path)).post("/api/projects", json={"name": "blocked"})
    assert response.status_code == 503
    assert "project metadata lock is unavailable" in response.json()["detail"]
    assert not (tmp_path / "projects.json").exists()


def test_delete_run_acquires_project_lock_before_removing_run_bytes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from looplab.events import eventstore
    from looplab.events.eventstore import EventStoreLockError
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    project_store = ProjectStore(tmp_path / "projects.json")
    project_store.set_label("demo", "keep")
    before = project_store.path.read_bytes()
    original_lock = eventstore._interprocess_lock

    @contextmanager
    def unavailable(path, *, required=False):
        if not required:
            with original_lock(path, required=required):
                yield
            return
        raise EventStoreLockError(path, OSError("locking unsupported"))
        yield  # pragma: no cover - contextmanager syntax only

    monkeypatch.setattr(eventstore, "_interprocess_lock", unavailable)
    response = TestClient(make_app(tmp_path)).delete("/api/runs/demo")
    assert response.status_code == 503
    assert "project metadata lock is unavailable" in response.json()["detail"]
    assert (run_dir / "events.jsonl").is_file(), "lock failure must precede irreversible deletion"
    assert project_store.path.read_bytes() == before


def test_delete_top_level_unassigns_runs(tmp_path):
    s = store(tmp_path)
    top = s.create("Top")
    s.assign("run_x", top.id)
    s.delete(top.id)
    assert s.project_of("run_x") is None             # no parent -> assignment removed
    assert s.load()["projects"] == []


# --------------------------------------------------------------- super-tasks (flat parallel axis)
def test_supertask_create_assign_and_orthogonal_to_projects(tmp_path):
    s = store(tmp_path)
    proj = s.create("Q3")                            # the two axes are independent
    st = s.create_supertask("nomad2018", task_id="nomad2018-predict-transparent-conductors")
    assert st["id"].startswith("st_") and st["name"] == "nomad2018"
    assert st["task_id"] == "nomad2018-predict-transparent-conductors"
    s.assign("run_a", proj.id)                        # project axis
    s.assign_supertask("run_a", st["id"])             # super-task axis (create_supertask returns a dict)
    assert s.project_of("run_a") == proj.id           # a run sits in BOTH at once
    assert s.supertask_of("run_a") == st["id"]
    s.assign_supertask("run_a", None)                 # clear only the super-task
    assert s.supertask_of("run_a") is None and s.project_of("run_a") == proj.id


def test_supertask_rename_and_unknown_id_guard(tmp_path):
    s = store(tmp_path)
    st = s.create_supertask("tmp")
    s.rename_supertask(st["id"], "MLE-bench")
    assert s.load()["supertasks"][0]["name"] == "MLE-bench"
    with pytest.raises(ProjectError):
        s.assign_supertask("run_a", "st_missing")     # assign to unknown -> rejected
    with pytest.raises(ProjectError):
        s.rename_supertask("st_missing", "x")
    with pytest.raises(ProjectError):
        s.delete_supertask("st_missing")


def test_supertask_delete_unassigns_its_runs(tmp_path):
    s = store(tmp_path)
    a, b = s.create_supertask("A"), s.create_supertask("B")
    s.assign_supertask("r1", a["id"])
    s.assign_supertask("r2", b["id"])
    s.delete_supertask(a["id"])
    assert s.supertask_of("r1") is None               # run kept, just unassigned
    assert s.supertask_of("r2") == b["id"]            # untouched
    assert [x["id"] for x in s.load()["supertasks"]] == [b["id"]]


def test_forget_drops_supertask_assignment(tmp_path):
    s = store(tmp_path)
    st = s.create_supertask("X")
    s.assign_supertask("run_z", st["id"])
    s.forget("run_z")                                 # called when a run dir is deleted
    assert s.supertask_of("run_z") is None


# --- projects.json load tolerance (malformed / wrong-typed) --------------------------------------

def test_projects_load_tolerates_non_dict_json(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text("[]", encoding="utf-8")      # valid JSON, wrong shape — must not raise AttributeError
    data = ProjectStore(p).load()
    assert isinstance(data, dict) and "projects" in data


def test_projects_load_coerces_wrong_typed_keys(tmp_path):
    # A hand-edited projects.json that IS a dict but has a wrong-typed inner key must be coerced to
    # the skeleton type for that key, not left to TypeError downstream (_index / assign).
    (tmp_path / "projects.json").write_text(json.dumps({"assignments": [], "projects": "oops"}))
    data = ProjectStore(tmp_path / "projects.json").load()
    assert data["assignments"] == {}     # list -> {} (skeleton)
    assert data["projects"] == []        # str  -> [] (skeleton)
    assert data["labels"] == {}          # missing key -> default


def test_projects_load_preserves_wellformed(tmp_path):
    good = {"projects": [{"id": "p1", "name": "X"}], "assignments": {"r1": "p1"},
            "labels": {}, "supertasks": [], "supertask_assignments": {}}
    (tmp_path / "p.json").write_text(json.dumps(good))
    assert ProjectStore(tmp_path / "p.json").load() == good
