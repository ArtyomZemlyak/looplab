"""Scheduled chat turns are executable, editable and bounded; no provider/network required."""
import pytest

from looplab.serve.assistant import WatchTools, build_tools, run_turn
from looplab.serve.assistant_monitor import MonitorControlTools
from looplab.serve.assistant_watch import SessionWatches, WatchRefusal, WatchService, WatchStore
from tests.test_assistant_endpoint import _FakeChatClient, _call, _final


def _arm(store, **kw):
    return store.arm(session="s1", instruction="Check demo training; repair only if needed",
                     trigger={"kind": "schedule", "every_s": 1800}, now=100, **kw)


def test_monitor_runs_real_assistant_turns_adjusts_cadence_and_finishes(tmp_path):
    store = WatchStore(tmp_path)
    record = _arm(store, mode="plan", max_wakeups=5)
    model = _FakeChatClient([
        _call("configure_monitor", {"stop": False, "reason": "Training is almost done",
                                    "every_s": 60, "instruction": "Check demo's final result"}),
        _final("Обучение идёт; проверю снова через минуту."),
        _call("configure_monitor", {"stop": True, "reason": "Final result verified"}),
        _final("Обучение завершено, монитор остановлен.")])
    appended = []
    calls = []

    def turn(rec, instruction):
        calls.append(rec)
        return run_turn(model, tmp_path, [], instruction, rec["mode"],
                        monitor_cycle=True, response_language="ru")

    service = WatchService(store, observe_run=lambda _: None, run_turn_fn=turn,
                           append_turn=lambda sid, msg: appended.append((sid, msg)))
    assert service.tick(now=1899) == [] and calls == []
    service.tick(now=1900)
    changed = store.get(record["id"])
    assert changed["trigger"]["every_s"] == 60 and changed["next_due"] == 1960
    assert changed["instruction"] == "Check demo's final result"
    assert changed["wakeups"] == 1 and changed["status"] == "armed"
    assert changed["expires_at"] == record["expires_at"] and changed["max_wakeups"] == 5
    service.tick(now=1960)
    done = store.get(record["id"])
    assert done["status"] == "done" and done["wakeups"] == 2
    assert service.tick(now=5000) == [] and len(calls) == 2
    assert all(rec["mode"] == "plan" for rec in calls)
    assert [msg["content"] for _, msg in appended] == [
        "Обучение идёт; проверю снова через минуту.", "Обучение завершено, монитор остановлен."]
    assert appended[-1][1]["monitor_control"]["stop"] is True
    assert "Russian (ru)" in model.turns[0][0]["content"]


def test_timer_is_one_shot_done_and_survives_a_fresh_store(tmp_path):
    tools = WatchTools(SessionWatches(WatchStore(tmp_path), "s1", "acceptEdits"))
    assert "armed" in tools.execute("watch_after", {"after_s": 1800, "instruction": "Check demo"})
    store = WatchStore(tmp_path)
    record = store.list()[0]
    assert record["max_wakeups"] == 1 and record["trigger"]["once"] is True
    assert record["waiting_for"] == "in 30 min (once)"
    assert record["expires_at"] > record["next_due"] + 80000
    calls = []
    service = WatchService(store, observe_run=lambda _: None,
                           run_turn_fn=lambda rec, instr: calls.append(rec) or {"ok": True, "reply": "Checked"},
                           append_turn=lambda *_: None)
    assert service.tick(now=record["next_due"] - 1) == []
    service.tick(now=record["next_due"])
    assert store.get(record["id"])["status"] == "done"
    service.tick(now=record["next_due"] + 1800)
    assert len(calls) == 1


def test_update_preserves_spend_identity_and_refuses_a_foreign_chat(tmp_path):
    store = WatchStore(tmp_path)
    record = _arm(store, mode="acceptEdits", principal="owner")
    store.update(record["id"], wakeups=3)
    assert store.configure(record["id"], session="other", changes={"every_s": 60}) is None
    changed = store.configure(record["id"], session="s1", changes={
        "every_s": 600, "instruction": "Check losses and report"}, now=500)
    assert changed["next_due"] == 1100 and changed["waiting_for"] == "every 10 min"
    assert changed["instruction"] == "Check losses and report"
    for key in ("id", "created", "mode", "principal", "expires_at", "max_wakeups"):
        assert changed[key] == record[key]
    assert changed["wakeups"] == 3
    # Legacy/incomplete records remain inspectable: unknown deadlines are never invented.
    partial = dict(changed)
    del partial["next_due"]
    del partial["expires_at"]
    store._write(partial)
    listed = WatchTools(SessionWatches(store, "s1", "acceptEdits")).execute("list_watches", {})
    assert "next_due=None" in listed and "expires_at=None" in listed


def test_update_fences_an_already_read_due_record(tmp_path):
    store = WatchStore(tmp_path)
    stale = _arm(store)
    store.configure(stale["id"], session="s1", changes={"every_s": 3600}, now=1900)
    calls = []
    service = WatchService(store, observe_run=lambda _: None,
                           run_turn_fn=lambda *_: calls.append(True), append_turn=lambda *_: None)
    assert service._wake(stale, observation={}, now=1900) is None
    assert calls == [] and store.get(stale["id"])["next_due"] == 5500


def test_waking_and_terminal_monitors_cannot_be_reconfigured(tmp_path):
    store = WatchStore(tmp_path)
    record = _arm(store)
    store.claim(record["id"], now=1900)
    with pytest.raises(WatchRefusal, match="only an armed"):
        store.configure(record["id"], session="s1", changes={"instruction": "new"})
    store.cancel(record["id"])
    with pytest.raises(WatchRefusal, match="only an armed"):
        store.configure(record["id"], session="s1", changes={"every_s": 60})


@pytest.mark.parametrize("changes", [{}, {"every_s": None}, {"every_s": True}, {"every_s": 2},
                                    {"every_s": float("nan")}, {"instruction": " "},
                                    {"mode": "bypassPermissions"}, {"max_wakeups": 500},
                                    {"expires_at": 999999999}, {"wakeups": 0}])
def test_invalid_edits_are_atomic(tmp_path, changes):
    store = WatchStore(tmp_path)
    record = _arm(store)
    with pytest.raises(WatchRefusal):
        store.configure(record["id"], session="s1", changes=changes)
    assert store.get(record["id"]) == record


def test_wakeup_can_only_control_its_current_monitor(tmp_path):
    tools = build_tools(tmp_path, mode="plan", monitor_cycle=True)
    names = {spec["function"]["name"] for spec in tools.specs()}
    assert "configure_monitor" in names
    assert not {"watch_every", "watch_after", "update_watch", "stop_watch", "write_file"} & names
    control = MonitorControlTools()
    assert "refused" in control.execute("configure_monitor", {"stop": False, "reason": "renew",
                                                              "max_wakeups": 500})
    assert control.monitor_controls == []
    assert "recorded" in control.execute("configure_monitor", {"stop": True, "reason": "verified"})
    assert "already recorded" in control.execute("configure_monitor", {"stop": False, "reason": "again"})


@pytest.mark.parametrize("result", [{"ok": False, "reply": "Provider unavailable"},
                                    {"ok": True, "reply": "Partial", "budget_exhausted": {"kind": "time"}},
                                    {"ok": True, "monitor_control": {"stop": "yes", "reason": "bad"}}])
def test_incomplete_or_invalid_monitor_turn_blocks_instead_of_repeating(tmp_path, result):
    store = WatchStore(tmp_path)
    record = _arm(store)
    service = WatchService(store, observe_run=lambda _: None, run_turn_fn=lambda *_: result,
                           append_turn=lambda *_: None)
    service.tick(now=1900)
    assert store.get(record["id"])["status"] == "blocked"
    assert store.get(record["id"])["wakeups"] == 1
    assert service.tick(now=10000) == []


def test_operator_stop_wins_over_a_monitor_reconfiguration_handoff(tmp_path):
    store = WatchStore(tmp_path)
    record = _arm(store)

    def turn(*_):
        store.cancel(record["id"])
        return {"ok": True, "reply": "checked", "monitor_control": {
            "stop": False, "reason": "continue", "every_s": 60}}

    service = WatchService(store, observe_run=lambda _: None, run_turn_fn=turn,
                           append_turn=lambda *_: None)
    service.tick(now=1900)
    assert store.get(record["id"])["status"] == "cancelled"
    assert service.tick(now=3000) == []


def test_configure_route_is_session_scoped_and_preserves_mode(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    monkeypatch.setattr(WatchService, "ensure_started", lambda _: None)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    other = client.post("/api/assistant/sessions", json={}).json()["id"]
    watch = client.post("/api/assistant/watches", json={"session": sid, "kind": "schedule",
        "every_s": 1800, "instruction": "check training"}).json()["watch"]
    path = f"/api/assistant/watches/{watch['id']}"
    assert client.patch(path, json={"session": other, "every_s": 60}).status_code == 404
    assert client.patch(path, json={"session": sid, "mode": "bypassPermissions"}).status_code == 400
    res = client.patch(path, json={"session": sid, "every_s": 600, "instruction": "check losses"})
    assert res.status_code == 200, res.text
    changed = res.json()["watch"]
    assert changed["mode"] == "plan" and changed["trigger"]["every_s"] == 600
    assert changed["expires_at"] == watch["expires_at"]
    client.delete(path)
    assert client.patch(path, json={"session": sid, "every_s": 60}).status_code == 400


@pytest.mark.parametrize("name,args", [
    ("watch_every", {"every_s": 1800, "instruction": "check"}),
    ("watch_after", {"after_s": 1800, "instruction": "check"}),
    ("watch_run", {"run": "demo", "until": ["finished"], "instruction": "check"}),
    ("watch_status", {"target": {"kind": "run", "run": "demo"}, "until": ["finished"],
                      "instruction": "check"}),
    ("work_until_done", {"goal": "check"})])
def test_agent_armed_watch_pins_the_actual_principal_not_model_arguments(tmp_path, name, args):
    from looplab.serve.principal import OWNER_PRINCIPAL
    store = WatchStore(tmp_path)
    tools = build_tools(tmp_path, mode="plan", principal=OWNER_PRINCIPAL,
                        watches=SessionWatches(store, "s1", "plan"))
    tools.execute(name, {**args, "principal": "anonymous"})
    assert store.list()[0]["principal"] == "owner"


def test_monitor_handoff_has_no_domain_authority():
    import ast
    from pathlib import Path
    import looplab.serve.assistant_monitor as module
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name.startswith(("looplab.events", "looplab.engine", "looplab.serve.routers",
                                    "looplab.tools.run_control")) for name in modules)


def test_read_only_chat_cannot_reauthor_a_monitor_with_wider_pinned_permissions(tmp_path):
    store = WatchStore(tmp_path)
    record = _arm(store, mode="acceptEdits")
    tools = WatchTools(SessionWatches(store, "s1", "plan"))
    out = tools.execute("update_watch", {"id": record["id"], "instruction": "write new files"})
    assert "switch the chat back" in out and store.get(record["id"]) == record


@pytest.mark.parametrize("owner_default", ["auto", "en"])
def test_router_wakeup_performs_an_actual_permitted_repair_and_posts_in_the_same_chat(
        tmp_path, monkeypatch, owner_default):
    # With an English owner default the chat's explicit Russian still wins on the wake-up, as it
    # does on an interactive turn.
    monkeypatch.setenv("LOOPLAB_OUTPUT_LANGUAGE", owner_default)
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    target = tmp_path / "training.cfg"
    target.write_text("lr=nan", encoding="utf-8")
    model = _FakeChatClient([
        _call("watch_every", {"every_s": 1800, "instruction": "Read training.cfg; fix invalid learning rate",
                              "max_wakeups": 48, "lifetime_s": 86400}),
        _final("Монитор включён на сутки: раз в 30 минут."),
        _call("read_file", {"path": str(target)}),
        _call("write_file", {"path": str(target), "content": "lr=0.01"}),
        _call("configure_monitor", {"stop": True, "reason": "Invalid learning rate repaired"}),
        _final("Исправлена некорректная скорость обучения. Монитор завершён.")])
    services = []
    original_init = WatchService.__init__

    def capture(service, *args, **kwargs):
        original_init(service, *args, **kwargs)
        services.append(service)

    monkeypatch.setattr(WatchService, "__init__", capture)
    monkeypatch.setattr(WatchService, "ensure_started", lambda _: None)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda *_a, **_kw: model)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "acceptEdits"}).json()["id"]
    response = client.post(f"/api/assistant/sessions/{sid}/message_stream", json={
        "instruction": "Следи за обучением каждые 30 минут", "response_language": "ru"})
    assert response.status_code == 200
    store = services[0].store
    record = store.list(session=sid)[0]
    assert record["principal"] == "local" and record["mode"] == "acceptEdits"
    services[0].tick(now=record["next_due"])
    assert target.read_text(encoding="utf-8") == "lr=0.01"
    assert store.get(record["id"])["status"] == "done"
    transcript = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert transcript[-1]["watch"]["id"] == record["id"]
    assert transcript[-1]["monitor_control"]["stop"] is True
    assert "Исправлена" in transcript[-1]["content"]
    assert "Russian (ru)" in model.turns[-1][0]["content"]


@pytest.mark.parametrize("mode,expected", [("auto", "done"), ("acceptEdits", "blocked")])
def test_scheduled_reset_uses_ordinary_command_service_and_permission_gate(tmp_path, mode, expected):
    from tests.test_run_control_tools import _run, _RecordingCommands
    from looplab.serve.routers.assistant import unattended_watch_approver
    events = _run(tmp_path / "demo")
    commands = _RecordingCommands(tmp_path)
    store = WatchStore(tmp_path)
    record = _arm(store, mode=mode)
    model = _FakeChatClient([
        _call("reset_node", {"run_id": "demo", "node_id": 1, "stage": "eval"}),
        _call("configure_monitor", {"stop": True, "reason": "Retry handed off"}),
        _final("Проверка завершена.")])
    declined = []

    def turn(rec, instruction):
        result = run_turn(model, tmp_path, [], instruction, rec["mode"], monitor_cycle=True,
                          alive_fn=lambda _: False, approver=unattended_watch_approver(declined),
                          command_service=commands)
        if declined:
            result["unattended_denied"] = declined
        return result

    service = WatchService(store, observe_run=lambda _: None, run_turn_fn=turn, append_turn=lambda *_: None)
    service.tick(now=1900)
    assert store.get(record["id"])["status"] == expected
    reset_events = [event for event in events.read_all() if event.type == "node_reset"]
    if mode == "auto":
        assert [call[1] for call in commands.calls] == ["node_reset"]
        assert len(reset_events) == 1 and reset_events[0].data["node_id"] == 1
        assert declined == []
    else:
        assert commands.calls == [] and reset_events == [] and declined
        assert "needs approval" in store.get(record["id"])["last_error"]
    assert service.tick(now=4000) == []
