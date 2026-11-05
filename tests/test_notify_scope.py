from __future__ import annotations

from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve.agent_events import (attach_notify_scope, notify_scope,
                                       read_notify_config, write_notify_config)
from looplab.serve.server import make_app

OWNER = {"X-LoopLab-Token": "owner-secret"}
AGENT = {"X-LoopLab-Token": "agent-secret"}


def test_attach_is_scoped_idempotent_and_preserves_hook_configuration(tmp_path):
    root = tmp_path / "runs"
    (root / "demo").mkdir(parents=True)
    (root / "other").mkdir()
    write_notify_config(root, {
        "command": ["true"], "vars": {"thread": "T9"}, "kinds": ["run_idle"],
        "runs": ["old-run"]}, "external")
    assert notify_scope(root, "demo")["excluded_by_run_filter"] is True
    attached = attach_notify_scope(root, "demo", "external")
    assert attached["included_hooks"] == ["external"]
    assert attached["excluded_by_run_filter"] is False
    attach_notify_scope(root, "demo", "external")
    config = read_notify_config(root, "external")
    assert config["runs"] == ["old-run", "demo"]
    assert config["vars"] == {"thread": "T9"}
    assert config["kinds"] == ["run_idle"]
    assert config["command"] == ["true"]
    assert notify_scope(root, "other")["excluded_by_run_filter"] is True


def test_scope_route_is_owner_attach_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    root = tmp_path / "runs"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "config.snapshot.json").write_text(
        Settings(backend="toy", external_harness=True).model_dump_json())
    (root / "demo" / "task.snapshot.json").write_text('{"kind": "quadratic"}')
    EventStore(root / "demo" / "events.jsonl").append(
        "run_started", {"run_id": "demo", "run_uid": "uid-demo", "task_id": "task",
                        "goal": "g", "direction": "max"})
    write_notify_config(root, {"command": ["true"], "runs": ["old-run"]}, "external")
    client = TestClient(make_app(root))
    excluded = client.get("/api/runs/demo/notify-scope", headers=AGENT)
    assert excluded.status_code == 200
    assert excluded.json()["excluded_by_run_filter"] is True
    assert client.post("/api/runs/demo/notify-scope/attach?hook=external",
                       headers=AGENT).status_code == 403
    attached = client.post("/api/runs/demo/notify-scope/attach?hook=external", headers=OWNER)
    assert attached.status_code == 200, attached.text
    assert attached.json()["included_hooks"] == ["external"]


def test_concurrent_scope_attach_preserves_both_runs(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from looplab.serve import agent_events
    agent_events.write_notify_config(tmp_path, {'command': ['echo', '{message}'], 'runs': ['original']}, 'external')
    original = agent_events.read_notify_config
    barrier = threading.Barrier(2)
    def synchronized_read(*args, **kwargs):
        value = original(*args, **kwargs)
        try:
            barrier.wait(timeout=.3)
        except threading.BrokenBarrierError:
            pass
        return value
    monkeypatch.setattr(agent_events, 'read_notify_config', synchronized_read)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda run: agent_events.attach_notify_scope(tmp_path, run, 'external'),
                      ['one', 'two']))
    assert set(original(tmp_path, 'external')['runs']) == {'original', 'one', 'two'}
