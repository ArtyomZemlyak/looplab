"""Waking an external agent (doc 78; audit 2026-10-14 L-01): LoopLab derives actionable events
from its own record into a durable outbox, serves them as a long poll, and runs the operator's
notification hook with debouncing, retries and a ledger — no agent-side bridge."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve import agent_events as ae
from looplab.serve.server import make_app


def _external_run(root, name="demo"):
    rd = root / name
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps(
        Settings(backend="toy", external_harness=True, max_nodes=3).model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "task", "goal": "g",
                                 "direction": "min"})
    return rd, store


def _ev(seq, type_, **data):
    return SimpleNamespace(seq=seq, type=type_, data=data)


class _Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_log_rows_become_actionable_events_with_failure_forensics():
    rows = ae.derive_log_events("r", "uid", [
        _ev(5, "node_evaluated", node_id=0, metric=0.41, generation=0),
        _ev(6, "stage_finished", node_id=1, name="train", status="fail", exit_code=-11),
        _ev(7, "stage_finished", node_id=1, name="prep", status="ok", exit_code=0),
        _ev(8, "node_failed", node_id=1, reason="crash", error="AssertionError: wrapper",
            failure_forensics={"headline": "SIGSEGV on ranks 3,5", "signals": [
                {"rank": 3, "signal": "SIGSEGV"}]}),
        _ev(9, "pause", reason="external_obligations"),
        _ev(10, "node_created", node_id=2),
    ])
    assert [r["kind"] for r in rows] == ["node_evaluated", "stage_failed", "node_failed",
                                         "run_paused"]
    failed = rows[2]
    assert "SIGSEGV on ranks 3,5" in failed["summary"]
    assert failed["details"]["forensics"]["signals"][0]["signal"] == "SIGSEGV"
    assert rows[0]["key"] == "uid:seq:5"


def test_progress_conditions_name_questions_obligations_and_idle_budget():
    progress = {"event_seq": 12, "at_node": 1, "evidence_revision": "e" * 64,
                "pending_checkpoints": [{"checkpoint_id": "c1", "phase_id": "train_monitor",
                                         "node_id": 0, "stage": "train"}],
                "candidate_blockers_if_expanding": [{"phase_id": "research"}],
                "finish_report_due": False, "finish_reviews_due": [],
                "next_step": {"code": "choose_direction"},
                "execution": {"engine_running": True}}
    rows = ae.derive_condition_events("r", "uid", progress, budget_left=2)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["monitor_question", "obligation_due", "run_idle"]
    assert rows[0]["key"] == "uid:checkpoint:c1"
    assert ae.derive_condition_events("r", "uid", progress, budget_left=0)[-1]["kind"] != "run_idle"
    busy = {**progress, "next_step": {"code": "inspect_pending"}}
    assert "run_idle" not in [r["kind"] for r in ae.derive_condition_events(
        "r", "uid", busy, budget_left=2)]


def test_outbox_dedupes_by_key_and_pages_by_cursor(tmp_path):
    first = ae.append_events(tmp_path, [
        {"run_id": "a", "kind": "node_evaluated", "key": "u:seq:1", "summary": "x"},
        {"run_id": "b", "kind": "node_failed", "key": "v:seq:1", "summary": "y"}])
    again = ae.append_events(tmp_path, [
        {"run_id": "a", "kind": "node_evaluated", "key": "u:seq:1", "summary": "x"},
        {"run_id": None, "kind": "gpu_free", "key": "gpu_free:1", "summary": "z"}])
    assert [r["id"] for r in first] == [0, 1] and [r["id"] for r in again] == [2]
    page = ae.read_outbox(tmp_path, after=0)
    assert [r["id"] for r in page["events"]] == [1, 2] and page["next_after"] == 2
    only_a = ae.read_outbox(tmp_path, after=-1, run_id="a")
    assert [r["kind"] for r in only_a["events"]] == ["node_evaluated", "gpu_free"]
    stale = ae.read_outbox(tmp_path, after=99)
    assert stale["stale_cursor"] is True and stale["next_after"] == 2


def test_pump_derives_events_from_a_live_external_run_once(tmp_path):
    root = tmp_path / "runs"
    rd, store = _external_run(root)
    app = make_app(root)
    pump = app.state.looplab.agent_events
    clock = _Clock()
    pump.clock = clock
    pump.tick()                         # first sight: watermark at the end, conditions only
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.5})
    clock.t += 5
    appended = pump.tick()
    assert "node_evaluated" in [r["kind"] for r in appended]
    clock.t += 40                       # nothing new: a re-derivation appends no duplicate
    assert not [r for r in pump.tick() if r["kind"] == "node_evaluated"]
    client = TestClient(app)
    body = client.get("/api/agent-events", params={"after": -1}).json()
    assert [e["kind"] for e in body["events"]].count("node_evaluated") == 1
    now = client.get("/api/agent-events").json()
    assert now["events"] == [] and now["next_after"] == body["events"][-1]["id"]


def test_long_poll_returns_as_soon_as_an_event_lands(tmp_path):
    root = tmp_path / "runs"
    _external_run(root)
    client = TestClient(make_app(root))
    cursor = client.get("/api/agent-events").json()["next_after"]

    def later():
        time.sleep(0.3)
        ae.append_events(root, [{"run_id": "demo", "kind": "run_idle", "key": "k1",
                                 "summary": "idle"}])

    threading.Thread(target=later).start()
    started = time.monotonic()
    body = client.get("/api/agent-events", params={"after": cursor, "timeout": 20}).json()
    assert body["events"][0]["kind"] == "run_idle" and not body["timed_out"]
    assert time.monotonic() - started < 10
    empty = client.get("/api/agent-events",
                       params={"after": body["next_after"], "timeout": 0.2}).json()
    assert empty["events"] == [] and empty["timed_out"] is True
    assert client.get("/api/agent-events", params={"kinds": "nope"}).status_code == 400
    assert client.get("/api/agent-events", params={"run_id": "missing"}).status_code == 404


def test_hook_debounces_retries_with_backoff_and_records_a_ledger(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    calls = []
    outcomes = [{"ok": False, "exit_code": 3, "error": "queue busy"},
                {"ok": True, "exit_code": 0}]

    def runner(argv, timeout):
        calls.append(argv)
        return outcomes.pop(0)

    srv = SimpleNamespace(root=root)
    pump = ae.AgentEventPump(srv, runner=runner, clock=_Clock())
    ae.write_notify_config(root, {"command": ["codex", "queue", "--thread", "{thread}",
                                              "--message", "{message}"],
                                  "vars": {"thread": "T-1"}, "debounce_s": 10,
                                  "retry_base_s": 30})
    ae.append_events(root, [{"run_id": "r", "kind": "node_failed", "key": "k1",
                             "summary": "node 0 failed: token sk-ant-api03-" + "A" * 40}])
    _rewrite_outbox_ts(root, pump.clock())
    assert pump.deliver() is None and not calls          # still inside the debounce window
    pump.clock.t += 11
    failed = pump.deliver()
    assert failed["status"] == "failed" and calls[0][:4] == ["codex", "queue", "--thread", "T-1"]
    assert "sk-ant-api03-" + "A" * 40 not in calls[0][5]  # the message is redacted
    assert "after=-1" in calls[0][5]
    pump.clock.t += 5
    assert pump.deliver() is None and len(calls) == 1      # backing off
    pump.clock.t += 30
    ok = pump.deliver()
    assert ok["status"] == "delivered" and len(calls) == 2
    assert pump.deliver() is None                           # nothing undelivered left
    statuses = [r["status"] for r in ae.ledger_rows(root)]
    assert statuses == ["delivered", "failed"]


def _rewrite_outbox_ts(root, ts):
    path = ae.events_dir(root) / ae.OUTBOX
    rows = ae._read_jsonl(path)
    path.write_text("".join(json.dumps({**r, "ts": ts}) + "\n" for r in rows))


def test_hook_gives_up_after_max_attempts_and_says_so_next_time(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    pump = ae.AgentEventPump(SimpleNamespace(root=root), clock=_Clock(),
                             runner=lambda argv, t: {"ok": False, "exit_code": 1, "error": "x"})
    ae.write_notify_config(root, {"command": ["notify", "{message}"], "debounce_s": 0,
                                  "max_attempts": 2, "retry_base_s": 1})
    ae.append_events(root, [{"run_id": "r", "kind": "run_idle", "key": "k", "summary": "idle"}])
    _rewrite_outbox_ts(root, pump.clock() - 1)
    pump.deliver()
    pump.clock.t += 5
    pump.deliver()
    assert [r["status"] for r in ae.ledger_rows(root)][:2] == ["abandoned", "failed"]
    seen = []
    pump.runner = lambda argv, t: seen.append(argv) or {"ok": True, "exit_code": 0}
    ae.append_events(root, [{"run_id": "r", "kind": "run_idle", "key": "k2", "summary": "idle"}])
    _rewrite_outbox_ts(root, pump.clock() - 1)
    pump.deliver()
    assert "could not be delivered" in seen[0][1]


def test_run_variables_route_per_run_threads_and_a_run_can_opt_out(tmp_path):
    root = tmp_path / "runs"
    for name in ("a", "b", "c"):
        (root / name).mkdir(parents=True)
    calls = []
    pump = ae.AgentEventPump(SimpleNamespace(root=root), clock=_Clock(),
                             runner=lambda argv, t: calls.append(argv) or {"ok": True,
                                                                         "exit_code": 0})
    ae.write_notify_config(root, {"command": ["q", "{thread}", "{message}"],
                                  "vars": {"thread": "default"}, "debounce_s": 0})
    ae.write_run_notify(root / "b", variables={"thread": "thread-b"})
    ae.write_run_notify(root / "c", enabled=False)
    ae.append_events(root, [{"run_id": n, "kind": "run_idle", "key": f"k{n}", "summary": n}
                            for n in ("a", "b", "c")])
    _rewrite_outbox_ts(root, pump.clock() - 1)
    pump.deliver()
    assert sorted(c[1] for c in calls) == ["default", "thread-b"]
    assert not any("[c]" in c[2] or "run c" in c[2] for c in calls)


def test_config_validation_refuses_shells_unknown_placeholders_and_bad_vars(tmp_path):
    with pytest.raises(ValueError):
        ae.validate_notify_config({"command": []})
    with pytest.raises(ValueError, match="undefined placeholders"):
        ae.validate_notify_config({"command": ["x", "{thread}"]})
    with pytest.raises(ValueError):
        ae.validate_notify_config({"command": ["x", "{message!r}"]})
    with pytest.raises(ValueError):
        ae.validate_notify_config({"command": ["x"], "vars": {"t": "a b; rm -rf /"}})
    config = ae.validate_notify_config({"command": "codex queue --thread {thread} --message "
                                                   "{message}", "vars": {"thread": "abc-1"}})
    assert config["command"][0] == "codex" and config["debounce_s"] == 15.0


def test_harness_token_may_set_run_vars_but_never_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-token-123456")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-token-654321")
    root = tmp_path / "runs"
    _external_run(root)
    client = TestClient(make_app(root))
    agent = {"X-LoopLab-Token": "agent-token-654321"}
    owner = {"X-LoopLab-Token": "owner-token-123456"}
    body = {"command": ["notify", "{message}"]}
    assert client.put("/api/agent-notify", json=body, headers=agent).status_code == 403
    assert client.post("/api/agent-notify/test", headers=agent).status_code == 403
    assert client.delete("/api/agent-notify", headers=agent).status_code == 403
    assert client.put("/api/agent-notify", json=body, headers=owner).status_code == 200
    r = client.put("/api/runs/demo/agent-notify", json={"vars": {"thread": "T9"}}, headers=agent)
    assert r.status_code == 200 and r.json()["notify"]["vars"] == {"thread": "T9"}
    assert client.put("/api/runs/demo/agent-notify", json={"vars": {"thread": "a b"}},
                      headers=agent).status_code == 400
    status = client.get("/api/agent-notify", headers=agent).json()
    assert status["configured"] is True and "node_failed" in status["kinds"]
    assert client.get("/api/runs/demo/agent-notify", headers=agent).json()["notify"]["vars"]
    monkeypatch.setattr(ae.AgentEventPump, "_run_command",
                        staticmethod(lambda argv, t: {"ok": True, "exit_code": 0}))
    tested = client.post("/api/agent-notify/test", headers=owner).json()
    assert tested["status"] in ("delivered", "failed")
    assert client.delete("/api/agent-notify", headers=owner).json()["ok"] is True


def test_hook_child_environment_never_carries_looplab_tokens(monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent")
    env = ae._child_env()
    assert "LOOPLAB_UI_TOKEN" not in env and "LOOPLAB_HARNESS_TOKEN" not in env


def test_real_command_runner_reports_exit_codes(tmp_path):
    import sys
    ok = ae.AgentEventPump._run_command([sys.executable, "-c", "print('hi')"], 30)
    bad = ae.AgentEventPump._run_command([sys.executable, "-c", "import sys; sys.exit(4)"], 30)
    missing = ae.AgentEventPump._run_command([str(tmp_path / "nope")], 30)
    assert ok["ok"] and bad["exit_code"] == 4 and not missing["ok"]


def test_mcp_wait_for_next_long_polls_the_outbox():
    import httpx
    from looplab.harness.mcp_server import HarnessAPI

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"events": [{"id": 4, "kind": "run_idle"}],
                                         "next_after": 4, "timed_out": False})

    api = HarnessAPI("http://127.0.0.1:8765", "tok", transport=httpx.MockTransport(handler))
    result = api.wait_for_next(3, 90, "demo", "run_idle,node_failed")
    assert result["status"] == 200 and result["body"]["next_after"] == 4
    assert "after=3" in seen["url"] and "timeout=90" in seen["url"] and "run_id=demo" in seen["url"]
    assert seen["timeout"]["read"] >= 90
    with pytest.raises(ValueError):
        api.wait_for_next(-5)
    with pytest.raises(ValueError):
        api.wait_for_next(1, 9999)


def test_agent_notify_and_events_cli(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app

    root = tmp_path / "runs"
    root.mkdir()
    runner = CliRunner()
    bad = runner.invoke(app, ["agent-notify", "--run-root", str(root), "--command", "x {thread}"])
    assert bad.exit_code != 0
    ok = runner.invoke(app, ["agent-notify", "--run-root", str(root), "--command",
                             "codex queue --thread {thread} --message {message}",
                             "--var", "thread=T1", "--debounce", "5"])
    assert ok.exit_code == 0, ok.output
    status = json.loads(ok.output)
    assert status["config"]["vars"] == {"thread": "T1"} and status["config"]["debounce_s"] == 5
    ae.append_events(root, [{"run_id": "r", "kind": "run_idle", "key": "k", "summary": "s"}])
    events = json.loads(runner.invoke(app, ["agent-events", "--run-root", str(root)]).output)
    assert events["events"][0]["kind"] == "run_idle"
    cleared = json.loads(runner.invoke(app, ["agent-notify", "--run-root", str(root),
                                             "--clear"]).output)
    assert cleared["configured"] is False


def test_named_agent_connections_deliver_independently(tmp_path):
    """doc 78 leftover (2026-10-14): one hook per run root could wake ONE agent. A named connection
    has its own command, vars, watermark and retries: a failing `codex` bridge neither blocks nor
    re-delivers what `claude` is told, and a new connection starts at the outbox's end."""
    root = tmp_path / "runs"
    (root / "r").mkdir(parents=True)
    ae.append_events(root, [{"run_id": "r", "kind": "run_idle", "key": "old", "summary": "old"}])
    calls = {"codex": [], "claude": []}

    def runner(argv, timeout):
        calls[argv[0]].append(argv)
        return ({"ok": False, "exit_code": 1, "error": "busy"} if argv[0] == "codex"
                else {"ok": True, "exit_code": 0})

    pump = ae.AgentEventPump(SimpleNamespace(root=root), clock=_Clock(), runner=runner)
    ae.write_notify_config(root, {"command": ["codex", "{thread}", "{message}"],
                                  "vars": {"thread": "C-1"}, "debounce_s": 0}, "codex")
    ae.write_notify_config(root, {"command": ["claude", "{message}"], "debounce_s": 0,
                                  "kinds": ["node_failed"]}, "claude")
    ae.write_run_notify(root / "r", variables={"thread": "C-run"}, hook="codex")
    ae.append_events(root, [{"run_id": "r", "kind": "node_failed", "key": "new", "summary": "boom"}])
    _rewrite_outbox_ts(root, pump.clock() - 1)
    pump.deliver()
    assert len(calls["codex"]) == 1 and calls["codex"][0][1] == "C-run"
    assert len(calls["claude"]) == 1 and "old" not in calls["claude"][0][1]   # starts from now
    assert "#1 node_failed" in calls["claude"][0][1]
    pump.clock.t += 1
    pump.deliver()
    assert len(calls["claude"]) == 1 and len(calls["codex"]) == 1     # codex backs off alone
    hooks = {r.get("hook") for r in ae.ledger_rows(root)}
    assert hooks == {"codex", "claude"}
    status = ae.notify_status(root)
    assert set(status["hooks"]) == {"codex", "claude"} and status["configured"] is False
    assert status["hooks"]["codex"]["delivery"]["attempts"] == 1
    with pytest.raises(ValueError):
        ae.write_notify_config(root, {"command": ["x"]}, "../evil")
    ae.clear_notify_config(root, "codex")
    assert set(ae.notify_status(root)["hooks"]) == {"claude"}
    assert ae.read_run_notify(root / "r", "codex")["vars"] == {"thread": "C-run"}
    assert "vars" not in ae.read_run_notify(root / "r")          # the default hook is untouched


def test_named_connection_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-token-123456")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-token-654321")
    root = tmp_path / "runs"
    _external_run(root)
    client = TestClient(make_app(root))
    agent = {"X-LoopLab-Token": "agent-token-654321"}
    owner = {"X-LoopLab-Token": "owner-token-123456"}
    body = {"command": ["claude-wake", "{message}"]}
    assert client.put("/api/agent-notify?hook=claude", json=body, headers=agent).status_code == 403
    assert client.put("/api/agent-notify?hook=../x", json=body, headers=owner).status_code == 400
    r = client.put("/api/agent-notify?hook=claude", json=body, headers=owner)
    assert r.status_code == 200 and r.json()["hook"] == "claude"
    r = client.put("/api/runs/demo/agent-notify", json={"hook": "claude", "enabled": False},
                   headers=agent)
    assert r.status_code == 200 and r.json()["notify"] == {"enabled": False}
    status = client.get("/api/agent-notify", headers=agent).json()
    assert "claude" in status["hooks"] and status["configured"] is False
    monkeypatch.setattr(ae.AgentEventPump, "_run_command",
                        staticmethod(lambda argv, t: {"ok": True, "exit_code": 0}))
    tested = client.post("/api/agent-notify/test?hook=claude", headers=owner).json()
    assert tested["hook"] == "claude" and tested["status"] in ("delivered", "failed")
    assert client.delete("/api/agent-notify?hook=claude", headers=owner).json()["ok"] is True
    assert "claude" not in client.get("/api/agent-notify", headers=owner).json()["hooks"]


def _question(rd, cid, *, observation="step 10 loss 1.20", node_id=0, answered=False):
    rows = [{"type": "question", "checkpoint_id": cid, "run_uid": "u", "node_id": node_id,
             "node_generation": 0, "phase_id": "train_monitor", "stage": "train",
             "observation": observation, "created_at": time.time()}]
    if answered:
        rows.append({"type": "answer", "checkpoint_id": cid, "verdict": "continue",
                     "answerer": "agent", "answered_at": time.time()})
    with open(rd / "harness_checkpoints.jsonl", "a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class _ConditionPump(ae.AgentEventPump):
    """The pump with its progress read replaced by the checkpoint journal: a question is a
    condition exactly while it has no answer (what `harness-progress` derives)."""

    def _conditions(self, rd, settings, state, events, run_uid):
        rows = ae._read_jsonl(rd / "harness_checkpoints.jsonl")
        answered = {r["checkpoint_id"] for r in rows if r.get("type") == "answer"}
        out = [{"run_id": rd.name, "run_uid": run_uid, "kind": "monitor_question",
                "key": f"{run_uid}:checkpoint:{r['checkpoint_id']}",
                "details": {"checkpoint_id": r["checkpoint_id"]}, "summary": "q"}
               for r in rows if r.get("type") == "question" and r["checkpoint_id"] not in answered]
        return [row for row in out if not self._routine_by_rules(rd, row)]


def test_a_question_answered_before_delivery_wakes_nobody(tmp_path):
    """Live check 2026-10-14 (minionerec-new-all-two-epochs-e7): the hook woke Codex for
    train_monitor questions that were already closed when the message arrived."""
    root = tmp_path / "runs"
    rd, _store = _external_run(root)
    _question(rd, "c1")
    calls = []
    pump = _ConditionPump(SimpleNamespace(root=root), clock=_Clock(),
                          runner=lambda argv, t: calls.append(argv) or {"ok": True, "exit_code": 0})
    ae.write_notify_config(root, {"command": ["wake", "{message}"], "debounce_s": 0})
    from looplab.events.replay import fold
    uid = fold(EventStore(rd / "events.jsonl").read_all()).run_uid or "demo"
    ae.append_events(root, [{"run_id": "demo", "kind": "monitor_question",
                             "key": f"{uid}:checkpoint:c1", "summary": "q1",
                             "details": {"checkpoint_id": "c1"}}])
    _question(rd, "c1", answered=True)                 # the agent (or the engine) closed it
    _rewrite_outbox_ts(root, pump.clock() - 1)
    assert pump.deliver() is None and calls == []
    assert ae.ledger_rows(root)[0]["status"] == "dropped_stale"
    # A still-open question IS delivered, and a burst of them is one line.
    for cid in ("c2", "c3", "c4"):
        _question(rd, cid)
    ae.append_events(root, [{"run_id": "demo", "kind": "monitor_question",
                             "key": f"{uid}:checkpoint:{cid}", "summary": f"q {cid}",
                             "details": {"checkpoint_id": cid}} for cid in ("c2", "c3", "c4")])
    _rewrite_outbox_ts(root, pump.clock() - 1)
    record = pump.deliver()
    assert record["status"] == "delivered" and len(calls) == 1
    assert "3 monitor questions; latest: q c4" in calls[0][1]
    assert calls[0][1].count("monitor_question") == 1


def test_a_question_the_declared_rules_call_routine_wakes_nobody(tmp_path):
    """An engine started before the rules shipped cannot apply them; the pump does not wake the
    agent for what the agent itself declared routine (and answers nothing)."""
    from looplab.harness.monitor_rules import write_rules
    root = tmp_path / "runs"
    rd, _store = _external_run(root)
    write_rules(rd, {"train_monitor": {"routine_if": {"loss_finite": True,
                                                       "progress_advances": True}}})
    _question(rd, "r1", observation="step 10 loss 1.20")
    _question(rd, "r2", observation="step 20 loss nan")       # not routine: loss not finite
    pump = _ConditionPump(SimpleNamespace(root=root), clock=_Clock())
    from looplab.events.replay import fold
    state = fold(EventStore(rd / "events.jsonl").read_all())
    rows = pump._conditions(rd, None, state, None, state.run_uid or "demo")
    assert [r["details"]["checkpoint_id"] for r in rows] == ["r2"]
    # Judged once: re-deriving does not compare the question's progress with itself.
    rows = pump._conditions(rd, None, state, None, state.run_uid or "demo")
    assert [r["details"]["checkpoint_id"] for r in rows] == ["r2"]
    assert not any(r.get("type") == "answer"
                   for r in ae._read_jsonl(rd / "harness_checkpoints.jsonl"))


def test_coalesce_keeps_one_question_per_run_and_every_fact():
    rows = [{"id": 1, "run_id": "a", "kind": "monitor_question", "summary": "x"},
            {"id": 2, "run_id": "a", "kind": "node_failed", "summary": "boom"},
            {"id": 3, "run_id": "b", "kind": "monitor_question", "summary": "y"},
            {"id": 4, "run_id": "a", "kind": "monitor_question", "summary": "z"}]
    out = ae.coalesce(rows)
    assert [r["id"] for r in out] == [2, 3, 4] and out[2]["coalesced"] == 2


def test_the_real_progress_path_drops_a_closed_question(tmp_path):
    """Through the pump's own condition read (harness-progress), not a stand-in."""
    from looplab.harness import checkpoints
    root = tmp_path / "runs"
    rd, store = _external_run(root)
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "", "files": {}})
    q = checkpoints.ask(rd, 0, 0, "train_monitor", stage="train", observation="step 5 loss 2.0")
    calls = []
    pump = ae.AgentEventPump(SimpleNamespace(root=root), clock=_Clock(),
                             runner=lambda argv, t: calls.append(argv) or {"ok": True,
                                                                         "exit_code": 0})
    pump.clock.t = time.time()
    appended = pump._scan_run(rd)
    assert "monitor_question" in [r["kind"] for r in appended]
    ae.write_notify_config(root, {"command": ["wake", "{message}"], "debounce_s": 0,
                                  "kinds": ["monitor_question"]})
    checkpoints.close_unanswered(rd, [q["checkpoint_id"]], "the evaluation ended")
    pump.clock.t += 5
    assert pump.deliver() is None and calls == []
    assert ae.ledger_rows(root)[0]["status"] == "dropped_stale"
